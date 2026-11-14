"""Background loops: mid-turn update outbox and the
one-shot restart handover. Each takes the discord client as a parameter so
the module has no bot globals."""
import json, os, pathlib, time, asyncio
from botcore import paths
import discord
from botcore.text import split_chunks


UPDATE_OUTBOX = paths.STATE / "update_outbox.jsonl"
_UPDATE_WORK  = paths.STATE / "update_outbox.processing.jsonl"


async def update_outbox_poller(client):
    """Deliver mid-task progress updates: a running turn calls `bin/discord-update`
    (which gets DISCORD_CHANNEL_ID in its env), queuing a line here; we post it to
    that exact channel/thread so the agent can send interim messages WITHOUT
    ending the turn. Plain sends (no mention) → quiet, unlike the final ping."""
    await client.wait_until_ready()
    print("update outbox poller started", flush=True)
    while not client.is_closed():
        try:
            if UPDATE_OUTBOX.exists() and UPDATE_OUTBOX.stat().st_size > 0:
                # Atomic claim so appends mid-flush land in a fresh outbox file.
                os.replace(UPDATE_OUTBOX, _UPDATE_WORK)
                for ln in _UPDATE_WORK.read_text().splitlines():
                    if not ln.strip():
                        continue
                    try:
                        evt = json.loads(ln)
                        cid = int(evt["channel_id"])
                        content = (evt.get("content") or "").strip()
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                        continue
                    if not content:
                        continue
                    chan = client.get_channel(cid)
                    if chan is None:
                        try:
                            chan = await client.fetch_channel(cid)
                        except discord.HTTPException:
                            chan = None
                    if chan:
                        for chunk in split_chunks(content):
                            try:
                                await chan.send(chunk)
                            except discord.HTTPException:
                                pass
                _UPDATE_WORK.unlink(missing_ok=True)
        except Exception as e:
            print(f"update outbox poller error: {e}", flush=True)
        await asyncio.sleep(3)


RESTART_HANDOVER = paths.STATE / "restart_handover.json"
_HANDOVER_WORK   = paths.STATE / "restart_handover.posting.json"


async def post_restart_handover(client):
    """If a restart left a handover note, announce we're back in the channel it
    names, then remove it so it fires exactly once. This lets the agent say
    "I'm back — finished X" across a restart it otherwise can't message through
    (its turn has ended by the time the bot comes back up). The note is written
    by `request-restart` / `dbot restart`, which also schedules the deferred restart.
"""
    await client.wait_until_ready()
    cid, content = 0, ""
    try:
        if RESTART_HANDOVER.exists():
            os.replace(RESTART_HANDOVER, _HANDOVER_WORK)  # claim atomically: fires once
        if _HANDOVER_WORK.exists():
            try:
                data = json.loads(_HANDOVER_WORK.read_text() or "null") or {}
                cid = int(data.get("channel_id") or 0)
                content = (data.get("content") or "").strip()
                ts = float(data.get("ts") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                cid, content, ts = 0, "", 0.0
            _HANDOVER_WORK.unlink(missing_ok=True)
            # Freshness guard: if a note's restart never fired and a LATER manual
            # restart finds it, don't post a stale message. 1h safely covers a
            # deferred restart that waited out a long turn (≤50min); older = discard.
            if (time.time() - ts) > 3600:
                cid, content = 0, ""
    except Exception as e:
        print(f"restart handover error: {e}", flush=True)
    try:
        posts = []
        if cid and content:
            lat = client.latency
            lat_ms = round(lat * 1000) if (lat is not None and lat == lat) else 0
            posts.append((cid, content + f"\n-# back online · gateway {lat_ms}ms"))
        for ch_id, text in posts:
            chan = client.get_channel(ch_id)
            if chan is None:
                try:
                    chan = await client.fetch_channel(ch_id)
                except discord.HTTPException:
                    chan = None
            if chan:
                for chunk in split_chunks(text):
                    try:
                        await chan.send(chunk)
                    except discord.HTTPException:
                        pass
        if posts:
            print("restart handover posted", flush=True)
    except Exception as e:
        print(f"restart handover error: {e}", flush=True)
