from dataclasses import dataclass

from .tokens import Identity


@dataclass(frozen=True)
class DemoMember:
    identity: Identity
    display_name: str


PRIMARY = DemoMember(Identity(user_id="user-primary", is_adult=True), "Demo Primary")
CHILD = DemoMember(Identity(user_id="user-child", is_adult=False), "Demo Child")
ADULT = DemoMember(Identity(user_id="user-adult", is_adult=True), "Demo Adult")
MEMBERS_BY_ID = {member.identity.user_id: member for member in (PRIMARY, CHILD, ADULT)}
