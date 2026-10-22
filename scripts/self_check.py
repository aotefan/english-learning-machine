"""One-click software voice diagnostic using existing setup and Opus smoke tools."""
import argparse
import asyncio
import datetime
import fcntl
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def pipeline_checks(events, result):
    """Report observed stages, never infer the quality or physical audibility."""
    by_name = {event['event']: event for event in events}
    stages = [('ASR中文识别', 'asr_result', 'text'),
              ('英文转换', 'english_result', 'text'),
              ('英文语音生成', 'tts_generated', 'packets'),
              ('音频返回与解码', 'audio_sent', 'return_ms')]
    checks = []
    blocked = False
    for label, name, field in stages:
        event = by_name.get(name, {})
        passed = field in event and event[field] is not None
        if field in ('text', 'packets'):
            passed = passed and bool(event[field])
        if name == 'audio_sent':
            passed = passed and result.get('output_frames', 0) > 0 and result.get('success') is True
        status = 'pass' if passed else 'not_run' if blocked else 'fail'
        detail = str(event[field]) if passed else '未观察到该阶段完成；请结合原始错误查看。'
        if passed and name == 'tts_generated':
            detail = f"已生成 {event[field]} 帧音频"
        elif passed and name == 'audio_sent':
            detail = f"已收到并解码 {result['output_frames']} 帧；返回用时 {event[field]:.0f} ms"
        checks.append({'stage':label, 'status':status, 'detail':detail})
        blocked = blocked or not passed
    return checks


async def command(args, timeout, output):
    # Reuse CLI preflight/fixture generation, keeping their output in the report folder.
    with output.open('wb') as log:
        process = await asyncio.create_subprocess_exec(*map(str, args), cwd=ROOT,
                    stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            async with asyncio.timeout(timeout):
                code = await process.wait()
        except BaseException:
            # Includes say/ffmpeg children launched for a missing synthetic fixture.
            import signal
            if process.returncode is None:
                try:os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:pass
                try:await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    try:os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:pass
                    await process.wait()
            raise
    if code:
        raise RuntimeError(f'命令退出码{code}，详细原因见 {output.name}')


def page_ready(port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/api/requests', timeout=3) as response:
        return response.status == 200


def save_report(output, report):
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    lines = ['英语终端 · Mac链路自检', '结果：'+('软件链路通过' if report['success'] else '未通过'),
             '输入为合成测试语音；未验证ESP32麦克风、Wi-Fi或喇叭，也未判断翻译准确率。']
    labels = {'pass':'通过', 'fail':'失败', 'warning':'提示', 'not_run':'未完成'}
    for check in report['checks']:
        lines.append(f"[{labels[check['status']]}] {check['stage']}：{check['detail']}")
    request = report.get('request', {})
    if request:
        lines.extend([f"request_id：{request.get('request_id', '未知')}",
                      f"中文：{request.get('asr', '无')}", f"英文：{request.get('english', '无')}"])
    if report.get('timings'):
        names = {'audio_upload_ms':'录音上传','asr_ms':'识别','english_ms':'翻译',
                 'tts_ms':'语音生成','return_ms':'音频返回','total_ms':'总计'}
        lines.append('耗时：'+' / '.join(f'{names[k]} {v:.0f} ms' for k,v in report['timings'].items()
                                      if k in names and isinstance(v,(int,float))))
    if report.get('error'):
        lines.append('原始错误：'+report['error'])
    lines.append('完整报告：'+str(output/'report.json'))
    text = '\n'.join(lines)+'\n'
    (output/'report.txt').write_text(text)
    return text


async def diagnose(output, service=None):
    report = {'hardware':False, 'success':False, 'checks':[], 'request':None,
              'started_at':datetime.datetime.now().astimezone().isoformat()}
    stage = '后端运行状态'
    detail = '请先双击“启动英语终端”，再执行自检。'
    try:
        from mac_service import MacService
        service = service or MacService()
        info = service.info()
        report['service'] = info
        if not info['loaded'] or not info['voice_ready']:
            raise RuntimeError('一键启动的语音后端尚未就绪。')
        report['checks'].append(dict(stage=stage, status='pass', detail=f"PID {info['pids']} 正在监听"))
        stage = '配置、模型文件与音频依赖'
        detail = '查看 preflight.log；按README补齐本地模型或依赖后重试。'
        args = [ROOT/'.venv/bin/python', ROOT/'scripts/run_backend.py', '--check', '--port', str(info['port'])]
        if info.get('vad_silence_ms') is not None:
            args += ['--vad-silence-ms', str(info['vad_silence_ms'])]
        await command(args, 120, output/'preflight.log')
        report['checks'].append(dict(stage=stage, status='pass', detail='复用现有启动前检查'))
        try:
            if not await asyncio.to_thread(page_ready, info['debug_port']):
                raise RuntimeError('HTTP response not ready')
            report['checks'].append(dict(stage='调试页', status='pass', detail='本地页面可访问'))
        except Exception as exc:
            report['checks'].append(dict(stage='调试页', status='warning', detail=f'{type(exc).__name__}；继续检查语音链路'))
        stage = '合成测试音频'
        detail = '查看 fixture.log；该步骤需要本机已有Tingting系统语音与FFmpeg。'
        folder = ROOT/'artifacts/synthetic'
        if not (folder/'01.wav').exists():
            folder = output/'input'
            await command([ROOT/'.venv/bin/python', ROOT/'scripts/make_fixtures.py',
                           '--count', '1', '--output', folder], 60, output/'fixture.log')
        from protocol_benchmark import read_input, connect, one
        _, metadata = read_input(folder, '01')
        report['input'] = metadata
        report['checks'].append(dict(stage=stage, status='pass', detail='本地合成“妈妈，我想喝水”，非真人录音'))
        stage = 'WebSocket握手'
        detail = '检查当前Mac地址及局域网访问策略；本工具不会修改网络设置。'
        host = '127.0.0.1' if info['host'] == '0.0.0.0' else info['host']
        async with asyncio.timeout(20):
            ws = await connect(f"ws://{host}:{info['port']}/xiaozhi/v1/")
        report['checks'].append(dict(stage=stage, status='pass', detail='复用原小智协议'))
        try:
            stage = '真实模型语音链路'
            detail = '查看原始错误及请求日志；若请求未返回ID，不猜测失败的模型阶段。'
            print('正在测试本地识别、英文转换和语音生成；首次加载可能较慢…', flush=True)
            async with asyncio.timeout(300):
                result = await one(ws, {'id':'01'}, folder, returned_dir=output/'returned')
            report['request'] = result
            if not result.get('request_id'):
                raise RuntimeError(result.get('error', '服务端没有返回request_id'))
            stage = '请求日志关联'
            detail = '已收到响应，但未取得完整匹配日志；检查报告中的event_log路径。'
            from soak_backend import wait_for_event, read_events
            terminal = await wait_for_event(SimpleNamespace(events=Path(info['event_log'])),
                                            0, 'request_complete', result['request_id'])
            events = [e for e in read_events(Path(info['event_log'])) if e.get('request_id')==result['request_id']]
            (output/'request-events.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in events))
            report['checks'] += pipeline_checks(events, result)
            report['timings'] = {k:v for k,v in terminal.items() if k.endswith('_ms')}
            report['success'] = (result['success'] and terminal.get('success') is True
                                 and all(c['status'] in ('pass','warning') for c in report['checks']))
            if not report['success']:
                report['error'] = result.get('error') or next((e.get('error') for e in events if e['event']=='error'), '请求未完整完成')
        finally:
            await ws.close()
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['checks'].append(dict(stage=stage, status='fail', detail=detail))
    finally:
        print(save_report(output, report), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New report directory; default artifacts/self-check/<unique>')
    args = parser.parse_args()
    base = ROOT/'artifacts/self-check'
    base.mkdir(parents=True, exist_ok=True)
    try:
        with (base/'run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            output = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='check-', dir=base))
            if args.output:output.mkdir(parents=True, exist_ok=False)
            report = asyncio.run(diagnose(output))
            return 0 if report['success'] else 1
    except BlockingIOError:
        print('已有自检正在运行，请等待完成。', file=sys.stderr)
    except KeyboardInterrupt:
        print('自检已取消；未停止语音后端。', file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f'无法创建自检报告：{exc}', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
