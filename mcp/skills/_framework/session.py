"""MCP stdio session lifecycle shared by skill runners.

A runner starts its own dedicated stdio MCP proxy for the run (same fleet
config as the interactive server) and keeps device leases alive with
periodic keep_alive calls. The 12s per-call timeout and the keep-alive
cadence mirror the proven jianhui values.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def load_spec(mcp_spec_path, repo):
    """Return the rustdesk mcpServers spec from an alternate file or the repo .mcp.json."""
    if mcp_spec_path is not None:
        return json.loads(Path(mcp_spec_path).read_text())['mcpServers']['rustdesk']
    spec = json.loads((Path(repo)/'.mcp.json').read_text())['mcpServers']['rustdesk']
    if not spec.get('env', {}).get('RUSTDESK_DEVICES_CONFIG'):
        raise ValueError('Fleet MCP configuration required')
    return spec


@contextlib.asynccontextmanager
async def open_session(spec, repo, env_extra=None):
    """Yield an initialized MCP ClientSession speaking to the fleet MCP."""
    params = StdioServerParameters(
        command=spec['command'], args=spec['args'], cwd=str(repo),
        env={**os.environ, **spec.get('env', {}), **(env_extra or {})})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            import asyncio
            await asyncio.wait_for(session.initialize(), 30)
            yield session


def make_call(session, stopping, timeout=12):
    """Return a call(name, device, **kwargs) coroutine factory with the
    standard invariants: refuse to send once a stop is requested, hard
    per-call timeout, MCP error surface as RuntimeError."""

    async def call(name, device, **kwargs):
        if stopping():
            raise InterruptedError('stop requested')
        result = await asyncio_wait_for_call(session, name, device, timeout, kwargs)
        if result.is_error:
            raise RuntimeError(str(result.content))
        return result.content
    return call


async def asyncio_wait_for_call(session, name, device, timeout, kwargs):
    import asyncio
    return await asyncio.wait_for(session.call_tool(name, {'device': device, **kwargs}), timeout)


def lease_keeper(call, devices, renew_every=60, minutes=5):
    """Return an async keeper(now) that renews keep_alive on all devices at
    most once per renew_every seconds of monotonic time."""

    state = {'until': 0.0}

    async def keep(now):
        if now >= state['until']:
            for dev in devices:
                await call('keep_alive', dev, minutes=minutes)
            state['until'] = time.monotonic()+renew_every
    return keep
