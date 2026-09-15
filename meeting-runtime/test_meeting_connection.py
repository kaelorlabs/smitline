import asyncio
import tempfile
import unittest
from pathlib import Path
from adapters_base import AuthenticationRequired, Capabilities
from meeting_connection import joined_meeting

class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_guest_closed_before_profile_retry_and_profile_never_leaks_to_guest(self):
        trace=[]
        class Browser:
            def __init__(self, env): self.env=env; trace.append(('create',env.copy()))
            async def __aenter__(self): return self
            async def __aexit__(self, *args): trace.append(('close', self.env.copy()))
            async def get_page(self): return self.env
        class Adapter:
            platform_id='teams'; capabilities=Capabilities()
            def __init__(self, url,page,stop,stage): self.env=page
            async def join(self, *args):
                if 'JOINLY_BROWSER_PROFILE_DIR' not in self.env: raise AuthenticationRequired()
            async def leave(self): trace.append(('leave', {}))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); (root/'teams-connected').touch()
            state={}
            async with joined_meeting('https://teams.microsoft.com/meet/123','Colleague','',{'JOINLY_BROWSER_PROFILE_DIR':'must-not-use'},asyncio.Event(),lambda _:None,state,profile_root=root,browser_factory=Browser,adapter_factory=Adapter):
                self.assertEqual([t[0] for t in trace],['create','close','create'])
                self.assertNotIn('JOINLY_BROWSER_PROFILE_DIR',trace[0][1])
                self.assertEqual(trace[2][1]['JOINLY_BROWSER_PROFILE_DIR'],str(root/'teams'))
                self.assertEqual(state['authenticationState'],'signed_in')
            self.assertEqual([t[0] for t in trace][-2:],['leave','close'])
