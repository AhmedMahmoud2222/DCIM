import { useEffect, useState } from "react";

/** Current time in ms, refreshed every `intervalMs` while the tab is visible. Ticks immediately on becoming visible again. */
export function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    let timer: number | undefined;
    const tick = () => setNow(Date.now());
    const start = () => { window.clearInterval(timer); tick(); timer = window.setInterval(tick, intervalMs); };
    const stop = () => { window.clearInterval(timer); timer = undefined; };
    const onVisibility = () => (document.visibilityState === "visible" ? start() : stop());
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);
    return () => { stop(); document.removeEventListener("visibilitychange", onVisibility); };
  }, [intervalMs]);
  return now;
}
