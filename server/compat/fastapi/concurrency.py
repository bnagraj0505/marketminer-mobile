import asyncio


async def run_in_threadpool(fn, *a, **k):
    return await asyncio.to_thread(fn, *a, **k)
