"""Local/server CLI usage report, without transcripts or credentials."""
import argparse
import json
import sqlite3
import math
import time
from pathlib import Path


ERROR_MARKERS = {
    'empty_audio': 'empty_audio', 'empty_or_silent_audio': 'empty_audio',
    'cloud_live_disabled': 'standby',
    'daily_cloud_budget_exhausted': 'budget_exhausted',
    'cloud_turn_rate_limited': 'rate_limited',
    'device_credential_revoked': 'credential_revoked',
    'english_input_exceeds_budget_bound': 'input_too_long',
    'seed_asr_failed': 'asr_failed', 'doubao_english': 'english_failed',
    'seed_tts_failed': 'tts_failed', 'TimeoutError': 'timeout',
}
ERROR_MESSAGES = {
    'empty_audio': '没有收到有效语音，请靠近麦克风说完整句后再试。',
    'standby': '云端处于待机，语音处理尚未启用。',
    'budget_exhausted': '今天的语音轮数或费用额度已用完；额度恢复或调整后再试。',
    'rate_limited': '请求间隔太短，请稍等几秒再说。',
    'credential_revoked': '设备连接凭据无效，需要检查设备授权。',
    'input_too_long': '这轮话语连同上下文过长，请分成短句再说。',
    'asr_failed': '语音识别服务未能完成这次请求，请稍后重试。',
    'english_failed': '英文回复生成失败，请稍后重试。',
    'tts_failed': '语音合成失败，这次未能完成英文播放，请稍后重试。',
    'timeout': '这次语音处理超时，请稍后重试。',
    'other_error': '处理失败，请提供请求编号以便排查。',
}


def classify_error(value):
    """Expose only known labels, never provider errors or credential-bearing text."""
    text = value if isinstance(value, str) else ''
    return next((code for marker, code in ERROR_MARKERS.items() if marker in text), 'other_error')


def monitor_report(path):
    """Recent authenticated cloud events, with bounded text and safe error labels."""
    now = time.time()
    with sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True) as db:
        records = db.execute(
            "SELECT time,request_id,event,data FROM events WHERE time>=? AND device_id!='cloud-smoke' ORDER BY rowid DESC LIMIT 2000",
            (now - 7 * 86400,)).fetchall()
    stages = {'recording_started':'收音中','audio_received':'识别中','asr_result':'生成回复中',
              'english_result':'生成语音中','tts_generated':'返回音频中',
              'audio_sent':'音频已发送','playback_started':'设备开始输出',
              'playback_completed':'播放队列已排空','error':'失败'}
    timing_keys = ('audio_upload_ms','asr_ms','english_ms','tts_ms','return_ms','total_ms')
    rows = {}
    last_event_at = None
    skipped = 0
    for stamp,rid,name,raw in reversed(records):
        if not isinstance(stamp,(int,float)) or not math.isfinite(stamp):
            skipped += 1
            continue
        last_event_at = max(last_event_at or stamp,stamp)
        if not isinstance(rid,str) or not rid:
            continue
        try:
            event = json.loads(raw)
            if not isinstance(event,dict):
                raise ValueError('invalid event')
        except (ValueError,TypeError):
            skipped += 1
            continue
        row = rows.setdefault(rid,{'request_id':rid,'time':stamp,'mode':None,'intent':None,
                                   'stage':'记录不完整','asr':None,'english':None,
                                   'error':None,'error_code':None,'playback_ack':False,'success':None,'timings':{}})
        row['time'] = max(row['time'],stamp)
        if name == 'recording_started':
            row['mode'] = event.get('mode')
        elif name == 'intent_classified':
            row['intent'] = event.get('intent') if event.get('intent') in (
                'express','chat','mode','repeat','slow','partial_repeat','hint',
                'explain','explain_only','clarify','correct','stop','no_change') else None
        elif name == 'asr_result':
            row['asr'] = event.get('text')
        elif name == 'english_result':
            row['english'] = event.get('text')
        elif name == 'playback_confirmed':
            row['playback_ack'] = True
        elif name == 'error':
            row['error_code'] = classify_error(event.get('error'))
            row['error'] = ERROR_MESSAGES[row['error_code']]
        if name == 'request_complete':
            row['success'] = event.get('success') is True
            row['stage'] = ('无需重复' if row['intent'] == 'no_change' else '链路完成') if row['success'] else '失败'
        elif name in stages and row['success'] is None and not row['error']:
            row['stage'] = stages[name]
        if row['error']:
            row['stage'] = '失败'
        for key in timing_keys:
            value = event.get(key)
            if type(value) in (int,float) and math.isfinite(value) and value >= 0:
                row['timings'][key] = value
    return {'requests':sorted(rows.values(),key=lambda item:item['time'],reverse=True)[:20],
            'read_at':now,'last_event_at':last_event_at,'truncated':len(records)==2000,
            'skipped_lines':skipped,'log_status':'ok'}


def diagnostics_report(path):
    """Bounded recent metadata only: no transcripts or arbitrary exception text."""
    fields = ('audio_duration_ms', 'asr_ms', 'english_ms', 'tts_ms', 'return_ms',
              'total_ms', 'stop_to_playback_ack_ms', 'vad_stop_ms', 'network_upload_ms',
              'asr_first_result_ms', 'llm_first_english_ms', 'tts_first_packet_ms',
              'stop_to_first_audio_ms', 'queue_wait_ms', 'speech_end_to_playback_ms')
    db = sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    try:
        # Rowid bounds work with the existing append-only event schema/indexes.
        rows = db.execute('SELECT request_id,time,event,data FROM events ORDER BY rowid DESC LIMIT 2000').fetchall()
    finally:
        db.close()
    requests = {}
    for rid, stamp, event, raw in rows:
        if not rid or (rid not in requests and len(requests) >= 100):
            continue
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict):
            continue
        result = requests.setdefault(rid, {'request_id': rid, 'time': stamp, 'success': None,
                                         'error_code': None, 'playback_reported': False, 'timings': {}})
        if event == 'request_complete' and result['success'] is None:
            result['success'] = item.get('success') is True
        if event == 'playback_confirmed':
            result['playback_reported'] = True
        if event == 'error' and result['error_code'] is None:
            result['error_code'] = classify_error(item.get('error'))
        for field in fields:
            value = item.get(field)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                result['timings'].setdefault(field, value)
    values = list(requests.values())
    timings = {}
    for field in fields:
        samples = sorted(r['timings'][field] for r in values if field in r['timings'])
        timings[field] = {'samples': len(samples),
                          'mean_ms': sum(samples)/len(samples) if samples else None,
                          'p95_ms': samples[math.ceil(.95*len(samples))-1] if samples else None}
    return {'sample_limit': 100, 'event_limit': 2000, 'requests_in_window': len(values),
            'completed': sum(r['success'] is not None for r in values),
            'succeeded': sum(r['success'] is True for r in values),
            'failed': sum(r['success'] is False for r in values),
            'timings': timings, 'recent': values[:20],
            'note': 'Recent retained events only; total includes recording and paced return; playback ack is not measured physical sound.'}


def billing_report(path):
    db=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    try:
        days=[r[0] for r in db.execute("SELECT DISTINCT date(time,'unixepoch') FROM billing_turns ORDER BY 1 DESC LIMIT 90")]
        stages={}
        for name in ('asr','english','tts'):
            row=db.execute("SELECT count(*) calls,COALESCE(sum(input_units),0) input_units,COALESCE(sum(output_units),0) output_units,COALESCE(sum(estimated_cny),0) estimated_cny,COALESCE(sum(status IN ('unknown','pending')),0) uncertain_calls FROM billing_stages WHERE stage=?",(name,)).fetchone()
            stages[name]=dict(row)
        daily=[]
        for day in days:
            totals={r['stage']:dict(r) for r in db.execute("SELECT stage,COALESCE(sum(estimated_cny),0) estimated_cny,sum(status IN ('unknown','pending')) uncertain_calls FROM billing_stages WHERE date(time,'unixepoch')=? GROUP BY stage",(day,))}
            row=db.execute("SELECT count(*) turns,COALESCE(sum(reserved_cny),0) reserved_cny FROM billing_turns WHERE date(time,'unixepoch')=?",(day,)).fetchone()
            daily.append({'day':day,**dict(row),'stages':totals})
        recent=[]
        for row in db.execute('SELECT * FROM billing_turns ORDER BY time DESC LIMIT 30'):
            recent.append({**dict(row),'stages':[dict(x) for x in db.execute('SELECT stage,status,input_units,output_units,estimated_cny FROM billing_stages WHERE request_id=?',(row['request_id'],))]})
        reservations=[]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='daily'").fetchone():
            reservations=[dict(utc_day=r[0],turns=r[1],reserved_cny=r[2]/1e6) for r in db.execute('SELECT day,turns,micros FROM daily ORDER BY day DESC LIMIT 90')]
        by_day={row['day']:row for row in daily}
        for reservation in reservations:
            day=reservation['utc_day']
            row=by_day.setdefault(day,{'day':day,'turns':0,'stages':{}})
            row['unattributed_reservations']=max(0,reservation['turns']-row['turns'])
            row['turns']=reservation['turns']
            row['reserved_cny']=reservation['reserved_cny']
        daily=sorted(by_day.values(),key=lambda row:row['day'],reverse=True)[:90]
        infrastructure=None
        snapshot=Path(path).parent/'infrastructure.json'
        if snapshot.exists():
            try:infrastructure=json.loads(snapshot.read_text())
            except (OSError,ValueError):pass
        return {'stages':stages,'days':daily,'recent':recent,'daily_reservations':reservations,
                'estimated_total_cny':sum(x['estimated_cny'] for x in stages.values()),
                'uncertain_calls':sum(x['uncertain_calls'] for x in stages.values()),
                'supplier_billed_cny':None,'supplier_bill_status':'not_connected',
                'currency':'CNY','timezone':'UTC','scope':'this_voice_server',
                'infrastructure_cost':infrastructure}
    finally:db.close()


def report(path):
    db=sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True)
    try:
        tables={x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        daily=[dict(utc_day=d, turns=n, reserved_cny=m/1e6)
               for d,n,m in db.execute('SELECT day,turns,micros FROM daily ORDER BY day DESC LIMIT 7')] if 'daily' in tables else []
        events={name:count for name,count in db.execute('SELECT event,count(*) FROM events GROUP BY event')}
        rows=[json.loads(x[0]) for x in db.execute("SELECT data FROM events WHERE event='request_complete' ORDER BY time DESC LIMIT 100")]
        timings={k: (sum(vals)/len(vals) if vals else None)
                 for k in ('asr_ms','english_ms','tts_ms','return_ms','total_ms')
                 for vals in [[r[k] for r in rows if isinstance(r.get(k),(int,float))]]}
        usage={}
        for timestamp,name,raw in db.execute("SELECT time,event,data FROM events WHERE event IN ('asr_usage','llm_usage','tts_usage','request_complete')"):
            from datetime import datetime, timezone
            day=datetime.fromtimestamp(timestamp,timezone.utc).date().isoformat()
            counts=usage.setdefault(day,dict(asr_seconds=0,llm_input_tokens=0,llm_output_tokens=0,
                tts_reported_characters=0,known_estimated_cny=0,requests_without_complete_cost=0))
            item=json.loads(raw)
            for field,total in (('input_audio_seconds','asr_seconds'),('input_tokens','llm_input_tokens'),
                                ('output_tokens','llm_output_tokens'),('billed_text_words','tts_reported_characters'),
                                ('estimated_cny','known_estimated_cny')):
                if isinstance(item.get(field),(int,float)):counts[total]+=item[field]
            if name=='request_complete' and item.get('estimated_cny') is None:
                counts['requests_without_complete_cost']+=1
        return {'daily_reservations':daily, 'events':events,'recent_mean_ms':timings,
                'daily_reported_usage':usage,
                'billing_note':'Conservative reservations, not provider invoice; total includes recording and paced output.'}
    finally:db.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--db',default='/state/usage.sqlite')
    print(json.dumps(report(p.parse_args().db),ensure_ascii=False,indent=2))
