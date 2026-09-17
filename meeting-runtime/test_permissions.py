import unittest

from agent_sessions import AgentSessionRef, MeetingPermissions
from permissions import (
    DEFAULT_PERMISSIONS, EXACT_CEILING, PORTAL_CEILING, bound_requested_permissions,
    is_narrower_or_equal, origin_ceiling, permission_mode,
)
from test_schemas import agent_session_payload, permissions_payload


class PermissionPolicyTests(unittest.TestCase):
    def test_defaults_are_read_only_and_fail_closed(self):
        self.assertEqual(DEFAULT_PERMISSIONS.workspace, 'read-only')
        self.assertEqual(DEFAULT_PERMISSIONS.commands, 'disabled')
        self.assertEqual(DEFAULT_PERMISSIONS.edits, 'disabled')
        self.assertEqual(DEFAULT_PERMISSIONS.network, 'disabled')
        self.assertEqual(DEFAULT_PERMISSIONS.commits, 'disabled')
        self.assertEqual(DEFAULT_PERMISSIONS.pushes, 'disabled')

    def test_origin_ceiling_uses_continuity_not_speech(self):
        exact = AgentSessionRef.from_dict(agent_session_payload())
        portal = AgentSessionRef.from_dict(agent_session_payload(
            sessionId='local-portal',
            metadata={'source': 'local-portal', 'continuity': 'context'},
        ))
        self.assertEqual(origin_ceiling(exact).to_dict(), EXACT_CEILING.to_dict())
        self.assertEqual(origin_ceiling(portal).to_dict(), PORTAL_CEILING.to_dict())

    def test_rejects_escalation_instead_of_silent_narrowing(self):
        portal = AgentSessionRef.from_dict(agent_session_payload(
            sessionId='local-portal',
            metadata={'source': 'local-portal', 'continuity': 'context'},
        ))
        with self.assertRaises(ValueError):
            bound_requested_permissions(permissions_payload(workspace='workspace-write'), portal)
        exact = AgentSessionRef.from_dict(agent_session_payload())
        with self.assertRaises(ValueError):
            bound_requested_permissions(permissions_payload(commands='allowed'), exact)
        allowed = bound_requested_permissions(permissions_payload(), exact)
        self.assertEqual(allowed.commands, 'approval-required')
        self.assertTrue(is_narrower_or_equal(DEFAULT_PERMISSIONS, EXACT_CEILING))

    def test_malformed_combinations_are_rejected(self):
        with self.assertRaises(ValueError):
            MeetingPermissions.from_dict(permissions_payload(
                workspace='none', commands='allowed'))
        with self.assertRaises(ValueError):
            MeetingPermissions.from_dict(permissions_payload(
                workspace='read-only', commits='approval-required'))
        self.assertEqual(permission_mode(permissions_payload(), 'network'), 'approval-required')
        self.assertEqual(permission_mode(permissions_payload(), 'unknown'), 'disabled')


if __name__ == '__main__':
    unittest.main()
