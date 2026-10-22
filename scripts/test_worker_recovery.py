"""A real model timeout kills the inference child; the next request must work."""
import json,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'upstream/backend/main/xiaozhi-server'))
from core.providers.local_worker import ModelWorker
if __name__=='__main__':
 w=ModelWorker();config={'engine':'marian','model_path':str(root/'models/english-marian'),'timeout_s':0.001}
 try:
  try:w.call('english','我要喝水',config);raise AssertionError('expected timeout')
  except TimeoutError:pass
  config['timeout_s']=90;start=time.perf_counter();text=w.call('english','我要喝水',config);assert text and 'water' in text.lower(),text
  result={'forced_timeout':True,'next_request_succeeded':True,'output':text,'recovery_request_ms':(time.perf_counter()-start)*1000}
  (root/'benchmark/worker-recovery.json').write_text(json.dumps(result,indent=2));print(result)
 finally:w.close()
