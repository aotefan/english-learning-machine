"""Real HTTP/1.1 keep-alive over localhost with synthetic provider JSON."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import tests.test_doubao_cloud as cloud
from core.providers.llm.doubao_english import doubao_english as provider


class KeepAliveTests(unittest.TestCase):
    def test_official_client_reuses_connection_across_normal_speech_gap(self):
        ports=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                ports.append(self.client_address[1])
                body=json.dumps({'id':'fixture','object':'chat.completion','created':1,'model':'fixture',
                    'choices':[{'index':0,'message':{'role':'assistant','content':'I want water.'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':12,'completion_tokens':5,'total_tokens':17}}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        ark=provider.Ark
        def local_ark(**kwargs):
            kwargs['base_url']=f'http://127.0.0.1:{server.server_port}/api/v3'
            return ark(**kwargs)
        try:
            with patch.object(provider,'Ark',side_effect=local_ark):
                llm=provider.LLMProvider({'api_key':'fixture','model_name':'fixture'})
            try:
                self.assertEqual(llm.response_result('我想喝水。','fixture')[0],'I want water.')
                time.sleep(5.2)  # Exceed the SDK's previous five-second idle pool expiry.
                self.assertEqual(llm.response_result('我想喝水。','fixture')[0],'I want water.')
                self.assertEqual(len(ports),2);self.assertEqual(ports[0],ports[1])
                self.assertEqual(llm.client.max_retries,0)
            finally:llm.close()
        finally:
            server.shutdown();server.server_close();worker.join(timeout=2)
