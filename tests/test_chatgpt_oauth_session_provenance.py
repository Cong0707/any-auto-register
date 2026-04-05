import unittest
from unittest import mock

from platforms.chatgpt.oauth_client import OAuthClient


def _ws(wid: str, org_id: str = "", **extra):
    item = {"id": wid}
    if org_id:
        item["organization"] = {"id": org_id}
    item.update(extra)
    return item


class OAuthSessionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.client = OAuthClient({}, proxy="", verbose=False)

    def test_score_session_data_provenance_prefers_richer_payload(self):
        poor = {"workspaces": [_ws("ws-1")]}
        rich = {
            "session_id": "sess-1",
            "openai_client_id": "client-1",
            "workspaces": [_ws("ws-1", "org-1", projects=[{"id": "proj-1"}])],
        }
        poor_score = self.client._score_session_data_provenance(poor, source="cookie")
        rich_score = self.client._score_session_data_provenance(rich, source="cookie")
        self.assertGreater(rich_score, poor_score)

    def test_select_session_data_with_overlap_merges_fields(self):
        cookie = {
            "session_id": "sess-cookie",
            "workspaces": [_ws("ws-1")],
        }
        html = {
            "openai_client_id": "client-html",
            "workspaces": [_ws("ws-1", "org-1"), _ws("ws-2")],
        }

        selected, reason = self.client._select_session_data_with_provenance(cookie, html)
        self.assertTrue(selected)
        self.assertIn("merged", reason)
        self.assertEqual(selected.get("session_id"), "sess-cookie")
        self.assertEqual(selected.get("openai_client_id"), "client-html")
        workspace_ids, _ = self.client._extract_workspace_org_ids_from_session(selected)
        self.assertEqual(workspace_ids, {"ws-1", "ws-2"})

    def test_select_session_data_conflict_can_choose_html(self):
        cookie = {
            "workspaces": [_ws("ws-cookie")],
        }
        html = {
            "session_id": "sess-html",
            "openai_client_id": "client-html",
            "workspaces": [_ws("ws-html", "org-html", projects=[{"id": "proj-1"}])],
        }

        selected, reason = self.client._select_session_data_with_provenance(cookie, html)
        self.assertTrue(selected)
        self.assertIn("choose_html", reason)
        self.assertEqual(selected.get("session_id"), "sess-html")
        workspace_ids, _ = self.client._extract_workspace_org_ids_from_session(selected)
        self.assertEqual(workspace_ids, {"ws-html"})

    def test_load_workspace_session_data_uses_cookie_fast_path(self):
        cookie = {
            "session_id": "sess-cookie",
            "openai_client_id": "client-cookie",
            "workspaces": [_ws("ws-1", "org-1", projects=[{"id": "proj-1"}])],
        }

        with mock.patch.object(self.client, "_decode_oauth_session_cookie", return_value=cookie), \
            mock.patch.object(self.client, "_fetch_consent_page_html") as fetch_html, \
            mock.patch.object(self.client, "_extract_session_data_from_consent_html") as parse_html:
            result = self.client._load_workspace_session_data(
                "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "UA",
                None,
            )

        self.assertEqual(result.get("session_id"), "sess-cookie")
        fetch_html.assert_not_called()
        parse_html.assert_not_called()

    def test_load_workspace_session_data_reconciles_cookie_and_html(self):
        cookie = {
            "workspaces": [_ws("ws-1")],
        }
        html_data = {
            "session_id": "sess-html",
            "openai_client_id": "client-html",
            "workspaces": [_ws("ws-1", "org-1")],
        }

        with mock.patch.object(self.client, "_decode_oauth_session_cookie", return_value=cookie), \
            mock.patch.object(self.client, "_fetch_consent_page_html", return_value="<html>mock</html>"), \
            mock.patch.object(self.client, "_extract_session_data_from_consent_html", return_value=html_data):
            result = self.client._load_workspace_session_data(
                "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "UA",
                None,
            )

        self.assertEqual(result.get("openai_client_id"), "client-html")
        workspace_ids, org_ids = self.client._extract_workspace_org_ids_from_session(result)
        self.assertEqual(workspace_ids, {"ws-1"})
        self.assertEqual(org_ids, {"org-1"})


if __name__ == "__main__":
    unittest.main()
