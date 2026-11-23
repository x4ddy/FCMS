from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor


OUTPUT = Path(__file__).with_name("FamilyCart_Authentication_Architecture.docx")
NAVY = "18324A"
PALE_BLUE = "EDF4F8"
LIGHT_GRAY = "F5F6F7"
BORDER = "D9D9D9"


def set_cell_fill(cell, color):
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), color)


def set_cell_margins(cell, top=110, start=120, bottom=110, end=120):
    properties = cell._tc.get_or_add_tcPr()
    margins = properties.first_child_found_in("w:tcMar")
    if margins is None:
        margins = OxmlElement("w:tcMar")
        properties.append(margins)
    for name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{name}"))
        if node is None:
            node = OxmlElement(f"w:{name}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def set_table_borders(table):
    properties = table._tbl.tblPr
    borders = properties.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        node = OxmlElement(f"w:{edge}")
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), "4")
        node.set(qn("w:color"), BORDER)
        borders.append(node)


def add_table(document, headers, rows, widths=None):
    table = document.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_table_borders(table)
    header = table.rows[0]
    header._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for index, text in enumerate(headers):
        cell = header.cells[index]
        set_cell_fill(cell, NAVY)
        set_cell_margins(cell)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        paragraph = cell.paragraphs[0]
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        run = paragraph.add_run(text)
        run.bold = True
        run.font.color.rgb = RGBColor(255, 255, 255)
        run.font.size = Pt(9)
    for row_index, values in enumerate(rows):
        cells = table.add_row().cells
        for index, text in enumerate(values):
            cell = cells[index]
            set_cell_fill(cell, PALE_BLUE if row_index % 2 else "FFFFFF")
            set_cell_margins(cell)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(0)
            run = paragraph.add_run(str(text))
            run.font.size = Pt(8.5)
    if widths:
        for row in table.rows:
            for index, width in enumerate(widths):
                row.cells[index].width = Inches(width)
    document.add_paragraph().paragraph_format.space_after = Pt(0)
    return table


def add_bullets(document, items):
    for item in items:
        paragraph = document.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(4)
        paragraph.add_run(item)


def add_numbered(document, items):
    for index, item in enumerate(items, start=1):
        paragraph = document.add_paragraph()
        paragraph.paragraph_format.left_indent = Inches(0.22)
        paragraph.paragraph_format.first_line_indent = Inches(-0.22)
        paragraph.paragraph_format.space_after = Pt(5)
        paragraph.add_run(f"{index}.  ")
        paragraph.add_run(item)


document = Document()
section = document.sections[0]
section.page_width = Inches(8.5)
section.page_height = Inches(11)
section.top_margin = Inches(0.72)
section.bottom_margin = Inches(0.72)
section.left_margin = Inches(0.78)
section.right_margin = Inches(0.78)

styles = document.styles
styles["Normal"].font.name = "Aptos"
styles["Normal"].font.size = Pt(10)
styles["Normal"].paragraph_format.space_after = Pt(7)
styles["Normal"].paragraph_format.line_spacing = 1.08
for style_name, size in (("Title", 26), ("Heading 1", 17), ("Heading 2", 12)):
    style = styles[style_name]
    style.font.name = "Aptos Display"
    style.font.size = Pt(size)
    style.font.color.rgb = RGBColor(0, 0, 0)
    style.font.bold = True
styles["Title"].paragraph_format.space_after = Pt(14)
title_style_properties = styles["Title"]._element.get_or_add_pPr()
title_border = title_style_properties.find(qn("w:pBdr"))
if title_border is not None:
    title_style_properties.remove(title_border)
styles["Heading 1"].paragraph_format.space_before = Pt(14)
styles["Heading 1"].paragraph_format.space_after = Pt(7)
styles["Heading 2"].paragraph_format.space_before = Pt(10)
styles["Heading 2"].paragraph_format.space_after = Pt(5)

title = document.add_paragraph(style="Title")
title.add_run("FamilyCart Authentication and Gateway Architecture")
subtitle = document.add_paragraph()
subtitle.paragraph_format.space_after = Pt(18)
run = subtitle.add_run("Implementation decision record for access control session management and service databases")
run.font.size = Pt(12)
run.font.color.rgb = RGBColor(70, 78, 84)

intro = document.add_paragraph()
intro.add_run("Decision. ").bold = True
intro.add_run(
    "All browser API and WebSocket traffic enters through the API Gateway. Auth verifies login or OTP, "
    "the Refresh service owns rotating sessions, and domain services remain private. The local build uses "
    "SQLite for repeatable development; production should move authoritative service data to PostgreSQL, "
    "use Redis only for ephemeral coordination, and use OpenSearch for search indexes."
)

document.add_heading("Implemented request path", level=1)
add_numbered(document, [
    "The web application sends login, refresh, cart, product, and WebSocket traffic to the API Gateway on port 8000.",
    "The Gateway applies request size limits, origin checks, rate limits, security headers, route allowlisting, JWT validation, and active-session validation.",
    "Auth validates the password or one-time code, creates the user when required, and starts a server-side session.",
    "Auth returns a signed access token in JSON and sets the opaque refresh token in an HttpOnly SameSite cookie.",
    "Protected requests carry the access token. The Gateway verifies its signature, issuer, audience, expiry, session identifier, and current server-side session state before forwarding.",
    "After an access-token 401, the browser calls the Refresh service through the Gateway once. Refresh rotates the token, records the old hash, and returns a new access token.",
    "Logout revokes the session family, clears the refresh cookie, and removes the browser access token. The Gateway rejects the previous access token immediately.",
])

document.add_heading("Token and session policy", level=1)
add_table(document, ["Control", "Decision", "Reason"], [
    ("Access token", "JWT valid for 10 minutes", "Limits exposure while avoiding refresh traffic on normal browsing."),
    ("Refresh token", "Opaque 384-bit secret valid for 30 days", "The browser cannot inspect it and the database stores only an HMAC hash."),
    ("Family lifetime", "Maximum 90 days", "Rotation cannot extend a stolen or abandoned session forever."),
    ("Storage", "HttpOnly Secure SameSite=Lax cookie", "Keeps the refresh credential out of JavaScript and reduces cross-site request risk."),
    ("Rotation", "Every successful refresh", "A replayed older token revokes the entire session family."),
    ("Logout", "Server revocation plus cookie deletion", "A stateless JWT cannot be deleted server-side, so the Gateway checks the session identifier."),
], widths=[1.35, 2.0, 3.55])

document.add_heading("Service responsibilities", level=1)
add_table(document, ["Service", "Owns", "Must not own"], [
    ("API Gateway", "Public routing, access checks, limits, security headers, request IDs", "Google credentials or domain records"),
    ("Auth", "Google ID-token verification, account activation, access-token issuance", "Refresh rotation or household authorization"),
    ("Refresh", "Session creation, refresh rotation, replay detection, logout revocation", "Passwords or cart permissions"),
    ("User", "Google identity, verified Gmail, account state, session records", "Household roles or carts"),
    ("Household", "Households, membership, roles, invitations", "Login credentials"),
    ("Cart", "Cart headers, items, contributions, revisions", "Product source of truth or payments"),
    ("Product and Search", "Catalogue source and derived search index", "User sessions"),
], widths=[1.2, 2.9, 2.8])

document.add_heading("User database model", level=1)
document.add_paragraph(
    "The user database contains Google identities, refresh sessions, and refresh-token history. "
    "Google ID tokens are verified and discarded; refresh credentials are stored only as keyed hashes. Session rows include creation, last-use, "
    "sliding expiry, absolute expiry, revocation, user agent, and IP metadata. Account status is checked when the "
    "Gateway validates a session."
)
add_table(document, ["Table", "Purpose", "Important constraints"], [
    ("users", "Identity and account status", "Unique Google subject and Gmail; explicit ACTIVE, LOCKED, or DISABLED state"),
    ("refresh_sessions", "Current refresh credential and session lifecycle", "One current hash, 30-day sliding and 90-day absolute expiry"),
    ("refresh_token_history", "Used-token detection", "Old hash retained per session to identify replay"),
], widths=[1.45, 2.6, 2.85])

document.add_page_break()
document.add_heading("Security controls", level=1)
add_bullets(document, [
    "Google ID tokens are verified with Google's official library, including signature, issuer, expiry, and audience checks.",
    "Refresh values are generated with a cryptographically secure generator and stored as HMAC-SHA256 hashes.",
    "JWTs require issuer, audience, subject, adult eligibility, unique token ID, issue time, expiry, and session ID claims.",
    "Refresh and logout are POST-only. Browser origins are allowlisted, and cookies use HttpOnly and SameSite. Secure cookies are mandatory in production.",
    "Sensitive gateway routes are rate-limited more strictly; production should move counters to Redis for consistency across replicas.",
    "The Gateway strips hop-by-hop headers, limits request bodies to 1 MiB, assigns request IDs, and sends restrictive browser security headers.",
    "Domain services must be private at the network layer so clients cannot bypass Gateway session revocation checks.",
    "Secrets belong in a managed secret store and logs must never include OTPs, refresh cookies, Authorization headers, or raw personal data.",
])

document.add_heading("Database recommendations", level=1)
add_table(document, ["Service", "Recommended production store", "Why"], [
    ("User Auth Refresh", "PostgreSQL plus Redis cache", "Transactions, uniqueness, auditability, and reliable session rotation. Redis accelerates active-session and rate-limit checks but is not the source of truth."),
    ("Household", "PostgreSQL", "Membership, roles, invitations, and ownership transfers require constraints and multi-row transactions."),
    ("Cart", "PostgreSQL initially", "The current contribution model is relational and transactional. Partition or add event streaming before considering a high-scale key-value store."),
    ("Product", "PostgreSQL", "Canonical SKU, price, restriction, and variant data needs constraints and versioned updates."),
    ("Search", "OpenSearch", "Designed for text search, ranking, facets, and denormalized read models. Rebuild it from Product events."),
    ("Inventory", "PostgreSQL initially", "Atomic stock reservations and reconciliation matter more than early horizontal scale. Partition by store when volume grows."),
    ("Address", "PostgreSQL plus Redis cache", "Structured ownership and updates are transactional; cache only frequently read resolved addresses."),
    ("Payments", "Dedicated PostgreSQL ledger", "Idempotency, reconciliation, immutable audit history, and outbox processing require strong durability. Store provider tokens, never card data."),
    ("Gateway", "Redis only", "Distributed rate limits, short-lived session-state cache, and replay protection. No durable business data."),
], widths=[1.18, 1.75, 3.97])

document.add_page_break()
document.add_heading("Credentials and provider keys", level=1)
document.add_paragraph(
    "No database API key is required for the current local build. SQLite files are created automatically. "
    "Redis is local and unauthenticated only for development. Before production deployment, provide the following through a secret manager rather than source control:"
)
add_bullets(document, [
    "A PostgreSQL connection URL and least-privilege database role for each service database.",
    "A TLS Redis URL with authentication for rate limits, session cache, and cart synchronization.",
    "A strong JWT signing key or, preferably, an asymmetric signing key managed by a cloud key service.",
    "An SMS provider key and approved sender identity for OTP delivery.",
    "Payment-provider credentials only when checkout is implemented.",
    "OpenSearch endpoint credentials when catalogue search moves beyond the current fixture data.",
])

document.add_heading("Production rollout sequence", level=1)
add_numbered(document, [
    "Deploy PostgreSQL user and session tables and migrate local SQLite data only if the local accounts must be retained.",
    "Place Auth, Refresh, Household, Cart, and Product on private networking; expose only the Gateway through TLS.",
    "Set secure cookies, production origins, managed signing keys, database roles, and log redaction before accepting real users.",
    "Move gateway rate limits and active-session caching to Redis while retaining PostgreSQL as the session source of truth.",
    "Connect the SMS provider, keep local OTP exposure disabled, and test delivery throttling and abuse monitoring.",
    "Add metrics for login failures, refresh replay, revoked-session access, gateway latency, and downstream availability.",
])

document.core_properties.title = "FamilyCart Authentication and Gateway Architecture"
document.core_properties.subject = "API gateway access tokens refresh sessions and database recommendations"
document.core_properties.author = "FamilyCart Engineering"
document.save(OUTPUT)
print(OUTPUT)
