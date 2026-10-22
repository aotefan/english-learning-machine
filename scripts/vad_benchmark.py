"""Real-time auto-listen check over existing Xiaozhi WS/Opus; not hardware acceptance."""
import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from protocol_benchmark import ROOT, connect, read_input, OpusEncoderUtils, opuslib_next


async def one_auto(ws, row, folder, output):
    pcm, metadata = read_input(folder, row['id'])
    packets = []
    # Keep leading/trailing silence explicit; the client never sends listen:stop.
    OpusEncoderUtils(16000, 1, 60).encode_pcm_to_opus_stream(
        bytes(32000) + pcm + bytes(64000), True, packets.append)
    await ws.send(json.dumps({'type': 'listen', 'state': 'start', 'mode': 'auto'}))
    started = time.perf_counter()
    stop_upload = asyncio.Event()
    stats = {'uplink_packets': 0, 'uplink_bytes': 0}

    async def upload():
        for packet in packets:
            if stop_upload.is_set():
                break
            await ws.send(packet)
            stats['uplink_packets'] += 1
            stats['uplink_bytes'] += len(packet)
            await asyncio.sleep(.06)

    sender = asyncio.create_task(upload())
    data = {'id': row['id'], 'hardware': False, 'mode': 'auto', **metadata}
    decoder = opuslib_next.Decoder(24000, 1)
    audio = []
    try:
        async with asyncio.timeout(180):
            while True:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    audio.append(decoder.decode(msg, 1440))
                    continue
                msg = json.loads(msg)
                if msg.get('request_id'):
                    data['request_id'] = msg['request_id']
                if msg['type'] == 'error':
                    data['error'] = msg['error']
                if msg['type'] == 'stt':
                    data['asr'] = msg['text']
                if msg['type'] == 'tts':
                    if msg['state'] == 'start':
                        stop_upload.set()
                        data.setdefault('auto_stop_after_start_ms', (time.perf_counter()-started)*1000)
                    if msg['state'] == 'sentence_start':
                        data['english'] = msg['text']
                    if msg['state'] == 'stop':
                        break
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
    output.mkdir(parents=True, exist_ok=True)
    if audio:
        with wave.open(str(output/(row['id']+'.wav')), 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
            wav.writeframes(b''.join(audio))
    data.update(stats, success=bool(audio) and 'error' not in data,
                output_frames=len(audio), total_ms=(time.perf_counter()-started)*1000)
    return data


async def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = json.loads((ROOT/'benchmark/sentences.json').read_text())[:args.count]
    ws = await connect(args.url)
    try:
        with (output/'results.jsonl').open('w') as out:
            for row in rows:
                result = await one_auto(ws, row, ROOT/'artifacts/synthetic', output/'audio')
                out.write(json.dumps(result, ensure_ascii=False)+'\n'); out.flush()
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if not result['success']:
                    raise RuntimeError('Automatic request failed; inspect saved evidence')
    finally:
        await ws.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='ws://127.0.0.1:8000/xiaozhi/v1/')
    parser.add_argument('--count', type=int, default=3, choices=range(1, 21))
    parser.add_argument('--output', required=True)
    asyncio.run(main(parser.parse_args()))
