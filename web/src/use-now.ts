import { useEffect, useState } from 'react';

/** Wall-clock ticker for relative times; re-renders the caller every `interval` ms. */
export function useNow(interval: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), interval);
    return () => clearInterval(timer);
  }, [interval]);
  return now;
}
