"""#43: Contacts can make and change a person, not only find one.

The only Contacts action was find_contacts. create_contact and update_contact go through Contacts'
scripting model: every write is read back, a person is changed only by a stable id (or a query that
names exactly one person), a field with several values is never guessed at, and both are undoable.
Contacts itself is faked here; the scripts ran against the real app in the E2E check.
"""
from __future__ import annotations

import tests._state_isolation  # noqa: F401  (must precede mcp_server imports)
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import app_adapters_more as more
from mcp_server import app_undo
from mcp_server.app_adapters import mac_app
from mcp_server.policy import Capability, resolve_risk

SETTINGS = SimpleNamespace()
PID = "BB561E8E-DF38-42D5-936C-D4FF05BD3785:ABPerson"


def person(**changes):
    base = {"id": PID, "name": "Ahmet Can Yılmaz", "first_name": "Ahmet Can", "last_name": "Yılmaz",
            "organization": "", "note": "", "emails": [["work", "ahmet@example.com"]],
            "phones": [["mobile", "+90 555 000 00 00"]]}
    return {**base, **changes}


class FakeContacts:
    """One person in a dict; read_person reads it, the scripts set what they say."""

    def __init__(self, start=None) -> None:
        self.people = {} if start is None else {start["id"]: dict(start)}
        self.scripts: list[str] = []

    def read(self, pid, timeout_s=10.0):
        found = self.people.get(pid)
        return dict(found) if found else None

    def run(self, script, **_kw):
        self.scripts.append(script)
        if "make new person" in script:
            self.people[PID] = person(organization="Deneme A.Ş." if "organization:" in script else "")
            return PID
        if "every person whose name is" in script:
            return ""
        current = self.people.get(PID)
        if current is None:
            return "GONE"
        if "delete item 1 of matches" in script:
            del self.people[PID]
            return "OK"
        if 'set organization of p to "Yeni Şirket"' in script:
            current["organization"] = "Yeni Şirket"
        if 'set organization of p to ""' in script:
            current["organization"] = ""
        return "OK"


class ContactsCase(unittest.TestCase):
    def setUp(self) -> None:
        env = patch.dict(os.environ, {"MAC_MCP_APP_UNDO": "1"})
        env.start()
        self.addCleanup(env.stop)

    def call(self, fake, **kwargs):
        with patch("mcp_server.app_adapters_more._run", side_effect=fake.run), \
                patch("mcp_server.app_undo._run", side_effect=fake.run), \
                patch.object(app_undo, "read_person", side_effect=fake.read):
            return mac_app(SETTINGS, app="Contacts", **kwargs)


class CreateTests(ContactsCase):
    def test_a_new_person_is_made_read_back_and_undoable(self) -> None:
        fake = FakeContacts()
        result = self.call(fake, action="create_contact", title="Ahmet Can Yılmaz", email="ahmet@example.com",
                           phone="+90 555 000 00 00", organization="Deneme A.Ş.")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertTrue(result["verified"])
        self.assertEqual(PID, result["contact"]["id"])
        script = next(s for s in fake.scripts if "make new person" in s)
        self.assertIn('first name:"Ahmet Can", last name:"Yılmaz"', script)
        self.assertIn('make new email at end of emails of p with properties {label:"work", value:"ahmet@example.com"}', script)
        self.assertIn("    save", script)
        self.assertTrue(result["undo"]["undoable"])
        undone = self.call(fake, action="undo", item_id=result["undo"]["undo_id"])
        self.assertTrue(undone["ok"] and undone["removed"] and undone["verified"], undone)
        self.assertEqual({}, fake.people)

    def test_a_replay_finds_the_person_instead_of_a_twin(self) -> None:
        fake = FakeContacts(person())
        with patch.object(more, "_people_named", return_value=[person()]):
            result = self.call(fake, action="create_contact", title="Ahmet Can Yılmaz", email="AHMET@example.com")
        self.assertTrue(result["duplicate"])
        self.assertFalse(result["created"])
        self.assertFalse(any("make new person" in s for s in fake.scripts))

    def test_a_name_is_required_and_bad_addresses_are_refused(self) -> None:
        fake = FakeContacts()
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         self.call(fake, action="create_contact", title=" ")["reason_code"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         self.call(fake, action="create_contact", title="Ada", email="not-an-address")["reason_code"])
        self.assertEqual([], fake.scripts)


class UpdateTests(ContactsCase):
    def test_one_field_changes_is_read_back_and_undone(self) -> None:
        fake = FakeContacts(person())
        result = self.call(fake, action="update_contact", item_id=PID, organization="Yeni Şirket")
        self.assertTrue(result["ok"] and result["verified"], result)
        self.assertEqual(["organization"], result["changed_fields"])
        self.assertEqual("Yeni Şirket", fake.people[PID]["organization"])
        change = next(s for s in fake.scripts if "set organization" in s)
        self.assertNotIn("first name", change)  # only what was asked
        undone = self.call(fake, action="undo", item_id=result["undo"]["undo_id"])
        self.assertTrue(undone["ok"] and undone["restored"], undone)
        restore = next(s for s in fake.scripts if "delete every email" in s)
        self.assertIn('make new email at end of emails of p with properties {label:"work", value:"ahmet@example.com"}', restore)
        self.assertEqual("", fake.people[PID]["organization"])

    def test_several_matching_people_change_nothing_and_are_listed(self) -> None:
        fake = FakeContacts(person())
        two = {"ok": True, "contacts": [
            {"id": "a:ABPerson", "name": "Ahmet Yılmaz", "organization": "", "emails": ["a@example.com"], "phones": []},
            {"id": "b:ABPerson", "name": "Ahmet Yılmaz", "organization": "Acme", "emails": [], "phones": []}]}
        with patch.object(more, "contacts_find", return_value=two):
            result = self.call(fake, action="update_contact", query="Ahmet Yılmaz", organization="Yeni Şirket")
        self.assertFalse(result["ok"])
        self.assertEqual("CONTACTS_PERSON_NOT_UNIQUE", result["reason_code"])
        self.assertEqual(["a:ABPerson", "b:ABPerson"], [c["id"] for c in result["candidates"]])
        self.assertEqual([], fake.scripts)

    def test_a_query_naming_one_person_is_enough(self) -> None:
        fake = FakeContacts(person())
        one = {"ok": True, "contacts": [{"id": PID, "name": "Ahmet Can Yılmaz", "organization": "",
                                         "emails": [], "phones": []}]}
        with patch.object(more, "contacts_find", return_value=one):
            result = self.call(fake, action="update_contact", query="Ahmet Can", organization="Yeni Şirket")
        self.assertTrue(result["ok"], result)
        self.assertEqual(PID, result["contact"]["id"])

    def test_which_of_several_emails_is_never_guessed(self) -> None:
        fake = FakeContacts(person(emails=[["work", "a@example.com"], ["home", "b@example.com"]]))
        result = self.call(fake, action="update_contact", item_id=PID, email="c@example.com")
        self.assertEqual("CONTACTS_FIELD_AMBIGUOUS", result["reason_code"])
        self.assertEqual(["a@example.com", "b@example.com"], result["emails"])
        self.assertFalse(any("set value" in s or "make new email" in s for s in fake.scripts))

    def test_the_only_email_is_replaced_and_a_first_phone_added(self) -> None:
        fake = FakeContacts(person(phones=[]))
        self.call(fake, action="update_contact", item_id=PID, email="new@example.com", phone="+90 532 111 22 33")
        change = next(s for s in fake.scripts if "set value" in s or "make new phone" in s)
        self.assertIn('set value of item 1 of emails of p to "new@example.com"', change)
        self.assertIn('make new phone at end of phones of p with properties {label:"mobile", value:"+90 532 111 22 33"}',
                      change)

    def test_nothing_to_change_or_no_target_says_so(self) -> None:
        fake = FakeContacts(person())
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         self.call(fake, action="update_contact", item_id=PID)["reason_code"])
        self.assertEqual("APP_ADAPTER_ARGUMENT_INVALID",
                         self.call(fake, action="update_contact", organization="X")["reason_code"])
        self.assertEqual("CONTACTS_PERSON_NOT_FOUND",
                         self.call(fake, action="update_contact", item_id="gone:ABPerson", organization="X")["reason_code"])


class PolicyTests(unittest.TestCase):
    def test_writes_are_local_data_writes_and_listed(self) -> None:
        for action in ("create_contact", "update_contact"):
            _, risk = resolve_risk("mac_app", {"app": "Contacts", "action": action})
            self.assertIn(Capability.LOCAL_WRITE, risk.capabilities, action)
            self.assertFalse(risk.destructive, action)
        caps = mac_app(SETTINGS, app="Contacts", action="capabilities")
        self.assertIn("create_contact", str(caps))


if __name__ == "__main__":
    unittest.main()
