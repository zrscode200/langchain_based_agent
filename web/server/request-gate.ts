/** Revoke new requests before observing backend quiescence during handoff. */
export function requestGate() {
  let paused = false, uncertain = false;
  const active = new Set<Promise<Response>>();
  return {
    async run(operation: () => Promise<Response>) {
      if (paused) return Response.json({ detail: 'Returning control to the terminal. Use the terminal to continue.' }, { status: 503 });
      const request = Promise.resolve().then(operation);
      active.add(request);
      try { return await request; } finally { active.delete(request); }
    },
    markUncertain() { uncertain = true; },
    async pause() { paused = true; await Promise.allSettled([...active]); if (uncertain) throw new Error('A backend operation was not confirmed. Keep terminal input paused.'); },
    resume() { paused = false; },
  };
}
