"""Carts that are being checked out, and what each role may do."""

from datetime import datetime, timedelta, timezone

from backend.services.familycart_service import checkout_session
from backend.services.familycart_service.app import app as cart_app
from backend.services.familycart_service.db import transaction
from backend.tests import test_checkout as base

API, HOUSEHOLD, CART, SHARED = base.API, base.HOUSEHOLD, base.REGULAR_CART, base.SHARED
CHECKOUT = f"{CART}/checkout"


class CheckoutSessionTests(base.CheckoutTests):
    """Reuses the checkout fixtures; the inherited tests are not run again."""

    def setUp(self):
        super().setUp()
        self.published = 0

        def publish_now():
            self.published += 1
        cart_app.state.live_sync.publish_now = publish_now

    def call(self, method, path, who, expected=200, **body):
        response = self.cart.request(method, path, headers=who, json=body or None)
        self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.content else None

    def tap(self, sku, delta, who, expected=200, cart=CART):
        return self.call("PATCH", f"{cart}/items/{sku}", who, expected, delta=delta)

    def contents(self, cart=CART, who=None):
        detail = self.call("GET", cart, who or self.owner)
        pick = lambda rows: {(row["sku"], row["contributor_id"]): row["quantity"] for row in rows}
        return pick(detail["contributions"]), pick(detail["waiting"])

    def expire(self):
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE checkout_sessions SET expires_at = ?",
                       ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))

    def order(self, address, skus=None, who=None, key="k"):
        quote = self.call("POST", f"{API}/checkout/quotes", who or self.owner, 201,
                          cart=SHARED, default_address=address, **({"skus": skus} if skus else {}))
        return self.place(quote, key, headers=who)

    def test_checkout_returns_the_latest_cart_and_publishes_at_once(self):
        self.grant_member()
        self.call("POST", CHECKOUT, self.owner, 409)  # nothing to check out
        self.tap("milk", 2, self.member)
        started = self.call("POST", CHECKOUT, self.owner)
        self.assertEqual([(row["sku"], row["quantity"]) for row in started["contributions"]], [("milk", 2)])
        self.assertEqual(started["checkout"]["user_id"], "user-primary")
        self.assertEqual(self.published, 1)
        self.call("POST", CHECKOUT, self.owner)  # renewing is quiet
        self.assertEqual(self.published, 1)
        problem = self.call("POST", CHECKOUT, self.member, 409)
        self.assertEqual(problem["detail"]["code"], "CHECKOUT_IN_PROGRESS")

    def test_items_added_during_checkout_start_a_new_cart_after_the_order(self):
        address = self.address(kind="household")
        self.grant_member()
        self.tap("milk", 2, self.owner)
        self.call("POST", CHECKOUT, self.owner)
        quote = self.quote(address, cart=SHARED)
        added = self.tap("milk", 1, self.member)
        self.assertEqual((added["quantity"], added["waiting"]), (3, True))
        self.tap("eggs", 3, self.owner)
        self.tap("eggs", -1, self.owner)
        self.assertEqual(self.contents(), (
            {("milk", "user-primary"): 2}, {("milk", "user-adult"): 1, ("eggs", "user-primary"): 2}))
        # The review is still valid: what it covers has not changed.
        placed = self.place(quote)
        self.assertEqual(placed.status_code, 201, placed.text)
        lines = [(line["sku"], line["quantity"]) for group in placed.json()["delivery_groups"] for line in group["lines"]]
        self.assertEqual(lines, [("milk", 2)])
        new = placed.json()["next_cart"]
        self.assertTrue(new["replaces"])
        path = f"{HOUSEHOLD}/carts/{new['id']}"
        self.assertEqual(self.contents(path), ({("milk", "user-adult"): 1, ("eggs", "user-primary"): 2}, {}))
        self.contents(path, self.member)  # the same people can use it
        self.call("GET", CART, self.owner, 404)  # the old cart is done
        ids = [cart["id"] for cart in self.call("GET", f"{HOUSEHOLD}/carts", self.member)]
        self.assertIn(new["id"], ids)
        # A tap that was already on its way to the old cart lands in the new one.
        self.tap("bread", 1, self.member)
        self.assertEqual(self.contents(path)[0][("bread", "user-adult")], 1)

    def test_unselected_items_keep_the_old_cart(self):
        address = self.address(kind="household")
        self.tap("milk", 1, self.owner)
        self.tap("eggs", 1, self.owner)
        self.call("POST", CHECKOUT, self.owner)
        self.tap("bread", 1, self.owner)
        placed = self.order(address, ["milk"])
        self.assertEqual(placed.status_code, 201, placed.text)
        new = placed.json()["next_cart"]
        self.assertFalse(new["replaces"])
        self.assertTrue(new["name"].endswith(" 2"))
        self.assertEqual(self.contents(), ({("eggs", "user-primary"): 1}, {}))
        self.assertEqual(self.contents(f"{HOUSEHOLD}/carts/{new['id']}")[0], {("bread", "user-primary"): 1})

    def test_without_waiting_items_the_cart_stays(self):
        address = self.address(kind="household")
        self.tap("milk", 1, self.owner)
        self.call("POST", CHECKOUT, self.owner)
        placed = self.order(address)
        self.assertNotIn("next_cart", placed.json())
        detail = self.call("GET", CART, self.owner)
        self.assertEqual((detail["contributions"], detail["checkout"]), ([], None))

    def test_leaving_or_abandoning_checkout_returns_waiting_items_to_the_cart(self):
        self.grant_member()
        self.tap("milk", 1, self.member)
        self.call("POST", CHECKOUT, self.owner)
        self.tap("milk", 2, self.member)
        self.call("DELETE", CHECKOUT, self.member, 403)
        self.call("DELETE", CHECKOUT, self.owner, 204)
        self.assertEqual(self.contents(), ({("milk", "user-adult"): 3}, {}))
        self.call("DELETE", CHECKOUT, self.owner, 204)  # nothing left to end
        self.call("POST", CHECKOUT, self.owner)
        self.tap("eggs", 1, self.member)
        self.expire()
        detail = self.call("GET", CART, self.member)
        self.assertEqual((detail["checkout"], detail["waiting"]), (None, []))
        self.assertEqual(len(detail["contributions"]), 2)
        # Somebody else can now check out.
        self.call("POST", CHECKOUT, self.member)

    def test_declined_payment_keeps_everything_waiting(self):
        address = self.address(kind="household")
        self.tap("milk", 1, self.owner)
        self.call("POST", CHECKOUT, self.owner)
        quote = self.quote(address, cart=SHARED)
        self.tap("eggs", 1, self.owner)
        self.assert_problem(self.place(quote, outcome="decline"), 402, "MOCK_PAYMENT_DECLINED")
        self.assertEqual(self.contents(), ({("milk", "user-primary"): 1}, {("eggs", "user-primary"): 1}))

    def test_items_being_checked_out_cannot_be_changed(self):
        self.grant_member()
        self.tap("milk", 2, self.member)
        self.call("POST", CHECKOUT, self.owner)
        self.tap("milk", -1, self.member, 409)
        self.call("DELETE", f"{CART}/items/milk", self.owner, 409)
        self.call("PUT", f"{CART}/items/milk/contributions/me", self.member, 409, quantity=1)
        self.call("DELETE", f"{CART}/items/milk/contributions/user-adult", self.owner, 409)
        self.call("DELETE", CART, self.owner, 409)
        self.assertEqual(self.call("PUT", f"{CART}/items/milk/contributions/me", self.member, quantity=5)["quantity"], 5)
        self.assertEqual(self.contents()[1], {("milk", "user-adult"): 3})
        self.assertEqual(self.call("PATCH", f"{CART}/items/milk/contributions/me", self.member, delta=1)["quantity"], 6)
        self.tap("milk", -9, self.member)  # only what waits can be taken back
        self.assertEqual(self.contents(), ({("milk", "user-adult"): 2}, {}))
        self.tap("eggs", 1, self.member)
        self.call("DELETE", f"{CART}/items/eggs", self.owner)
        self.assertEqual(self.contents()[1], {})

    def test_list_items_sent_during_checkout_wait(self):
        self.tap("milk", 1, self.owner)
        listed = self.call("POST", f"{HOUSEHOLD}/lists", self.owner, 201, name="L")["id"]
        item = self.call("POST", f"{HOUSEHOLD}/lists/{listed}/items", self.owner, 201, sku="eggs", quantity=2)["id"]
        self.call("POST", CHECKOUT, self.owner)
        moved = self.call("POST", f"{HOUSEHOLD}/lists/{listed}/move-to-cart", self.owner, target="regular-cart", item_ids=[item])
        self.assertEqual(moved["moved"], [item])
        self.assertEqual(self.contents()[1], {("eggs", "user-primary"): 2})

    def test_another_member_cannot_order_a_cart_being_checked_out(self):
        self.grant_member()
        mine = self.address(kind="household", headers=self.member)
        self.tap("milk", 1, self.owner)
        quote = self.quote(mine, cart=SHARED, headers=self.member)
        self.call("POST", CHECKOUT, self.owner)
        self.assert_problem(self.place(quote, headers=self.member), 409, "QUOTE_STALE")
        self.assert_problem(self.review(mine, cart=SHARED, headers=self.member), 409, "CHECKOUT_IN_PROGRESS")

    def test_removing_a_member_ends_their_checkout(self):
        self.grant_member()
        self.tap("milk", 1, self.owner)
        self.call("POST", CHECKOUT, self.member)
        self.tap("eggs", 1, self.owner)
        self.call("DELETE", f"{HOUSEHOLD}/members/user-adult", self.owner, 204)
        self.assertEqual(self.contents(), ({("milk", "user-primary"): 1, ("eggs", "user-primary"): 1}, {}))

    def test_primary_users_check_out_with_their_own_address_and_children_cannot(self):
        self.grant_member()
        self.call("PUT", f"{CART}/grants/user-child", self.owner, 200)
        admins = self.address(kind="household")
        mine = self.address(kind="household", headers=self.member)
        self.tap("milk", 1, self.minor)  # children can add
        self.assertFalse(self.call("GET", CART, self.minor)["can_checkout"])
        self.call("POST", CHECKOUT, self.minor, 403)
        self.assertEqual(self.review(mine, cart=SHARED, headers=self.minor).status_code, 403)
        self.assertTrue(self.call("GET", CART, self.member)["can_checkout"])
        self.call("POST", CHECKOUT, self.member)
        self.assert_problem(self.review(admins, cart=SHARED, headers=self.member), 404, "ADDRESS_NOT_FOUND")
        placed = self.order(mine, who=self.member)
        self.assertEqual(placed.status_code, 201, placed.text)
        self.assertEqual(placed.json()["purchaser_id"], "user-adult")

    def test_access_is_needed_to_check_out(self):
        mine = self.address(kind="household", headers=self.member)
        self.tap("milk", 1, self.owner)
        self.call("DELETE", f"{CART}/grants/user-adult", self.owner, 200)
        self.call("POST", CHECKOUT, self.member, 403)
        self.assert_problem(self.review(mine, cart=SHARED, headers=self.member), 404, "CART_NOT_FOUND")

    def test_only_the_admin_adds_and_removes_members_and_primary_users_can_ask(self):
        requests = f"{HOUSEHOLD}/member-requests"
        for who in (self.member, self.minor):
            self.call("POST", f"{HOUSEHOLD}/invitations", who, 403)
            self.call("DELETE", f"{HOUSEHOLD}/members/user-child", who, 403)
        self.call("POST", requests, self.minor, 403, name="Friend")
        self.call("POST", requests, self.owner, 409, name="Friend")
        asked = self.call("POST", requests, self.member, 201, name="Grandma", note="Lives with us")
        self.assertEqual([item["id"] for item in self.call("GET", requests, self.owner)], [asked["id"]])
        self.assertEqual(self.call("GET", requests, self.minor), [])
        decide = f"{requests}/{asked['id']}"
        self.call("PUT", decide, self.member, 403, approved=True)
        self.call("POST", f"{decide}/invitation", self.member, 404)  # not approved yet
        self.call("PUT", decide, self.owner, 204, approved=True)
        self.call("PUT", decide, self.owner, 404, approved=False)
        self.call("POST", f"{decide}/invitation", self.owner, 404)
        code = self.call("POST", f"{decide}/invitation", self.member, 201)["invitation_code"]
        self.call("POST", f"{decide}/invitation", self.member, 404)  # once only
        joined = self.call("POST", f"{API}/household-invitations/accept", self.user("user-grandma"), 200, invitation_code=code,
                           display_name="Grandma", address={"label": "Home", "recipient_name": "G", "address_line1": "1", "city": "Dubai", **base.DUBAI})
        self.assertEqual(joined["role"], "member")
        refused = self.call("POST", requests, self.member, 201, name="Other")
        self.call("PUT", f"{requests}/{refused['id']}", self.owner, 204, approved=False)
        self.assertEqual(self.call("GET", requests, self.member), [])

    def test_members_can_leave_and_the_admin_cannot(self):
        self.grant_member()
        self.tap("milk", 1, self.member)
        self.call("POST", CHECKOUT, self.member)
        self.tap("eggs", 1, self.owner)
        leave = f"{HOUSEHOLD}/leave"
        self.call("POST", leave, self.owner, 409)
        self.call("POST", leave, self.user("stranger"), 404)
        self.call("POST", leave, self.member, 204)
        self.call("GET", f"{HOUSEHOLD}/carts", self.member, 403)
        self.assertNotIn("demo-household", [item["id"] for item in self.call("GET", f"{API}/households", self.member)])
        # Their checkout ended and what they added stays.
        self.assertEqual(self.contents(), ({("milk", "user-adult"): 1, ("eggs", "user-primary"): 1}, {}))
        self.call("POST", leave, self.minor, 204)  # children can leave too

    def test_only_the_admin_can_delete_the_household(self):
        address = self.address(kind="household")
        self.grant_member()
        self.tap("milk", 1, self.owner)
        placed = self.order(address).json()
        self.tap("eggs", 1, self.member)
        self.call("POST", CHECKOUT, self.member)
        listed = self.call("POST", f"{HOUSEHOLD}/lists", self.member, 201, name="L")["id"]
        self.call("POST", f"{HOUSEHOLD}/lists/{listed}/items", self.member, 201, sku="eggs")
        code = self.call("POST", f"{HOUSEHOLD}/invitations", self.owner, 201)["invitation_code"]
        for who in (self.member, self.minor):
            self.call("DELETE", HOUSEHOLD, who, 403)
        self.call("DELETE", HOUSEHOLD, self.owner, 204)
        self.assertEqual(self.published, 3)  # the order, opening checkout, and this
        for who in (self.owner, self.member):
            self.assertEqual(self.call("GET", f"{HOUSEHOLD}/carts", who, 404)["detail"], "This household was deleted by its admin.")
            self.assertEqual(self.call("GET", f"{API}/households", who), [])
        self.call("DELETE", HOUSEHOLD, self.owner, 404)
        self.call("POST", f"{API}/household-invitations/accept", self.user("late"), 400, invitation_code=code, display_name="Late",
                  address={"label": "Home", "recipient_name": "L", "address_line1": "1", "city": "Dubai", **base.DUBAI})
        with transaction(self.db) as db:
            for table in ("household_addresses", "shopping_list_items", "cart_item_contributions", "checkout_sessions"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE household_id = 'demo-household'").fetchone()["n"], 0, table)
        self.assertEqual(self.call("GET", f"{API}/orders/{placed['order_id']}", self.owner)["total_fils"], placed["total_fils"])

    def test_children_never_see_tobacco(self):
        products = self.call("GET", f"{API}/products?household_id=demo-household", self.minor)
        tobacco = [p for p in cart_app.state.products.values() if p.category == "Tobacco"]
        self.assertTrue(tobacco)
        self.assertFalse({p.sku for p in tobacco} & {p["sku"] for p in products})
        self.assertNotIn("Tobacco", {p["category"] for p in products})
        self.call("GET", f"{API}/products/{tobacco[0].sku}", self.minor, 404)
        self.call("PUT", f"{CART}/grants/user-child", self.owner, 200)
        self.tap("milk", 1, self.owner)
        self.call("POST", CHECKOUT, self.owner)
        self.tap(tobacco[0].sku, 1, self.minor, 404)
        # Even if one is waiting, a child does not see it.
        with transaction(self.db, write=True) as db:
            checkout_session.set_waiting(db, "demo-household", "regular-cart", tobacco[0].sku, "user-primary", 1)
        self.assertEqual(self.contents(who=self.minor)[1], {})


for name in [name for name in dir(base.CheckoutTests) if name.startswith("test_")]:
    if name not in vars(CheckoutSessionTests):
        setattr(CheckoutSessionTests, name, None)
del name
