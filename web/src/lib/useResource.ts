import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

export interface Resource<T> {
  data: T | undefined;
  error: ApiError | null;
  loading: boolean;
  reload: () => Promise<void>;
}

/** Loads data on mount and when deps change; optional silent polling. */
export function useResource<T>(load: () => Promise<T>, deps: unknown[] = [], opts: { interval?: number } = {}): Resource<T> {
  const [state, setState] = useState<{ data: T | undefined; error: ApiError | null; loading: boolean }>({
    data: undefined,
    error: null,
    loading: true,
  });
  const loadRef = useRef(load);
  loadRef.current = load;
  const seq = useRef(0);
  const alive = useRef(true);

  const run = useCallback(async (silent: boolean) => {
    const id = ++seq.current;
    if (!silent) setState((s) => ({ ...s, loading: true }));
    try {
      const data = await loadRef.current();
      if (alive.current && id === seq.current) setState({ data, error: null, loading: false });
    } catch (e) {
      if (alive.current && id === seq.current) {
        const error = e instanceof ApiError ? e : new ApiError(0, (e as Error).message);
        setState((s) => ({ data: s.data, error, loading: false }));
      }
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void run(false);
    const timer = opts.interval ? setInterval(() => void run(true), opts.interval) : undefined;
    return () => {
      alive.current = false;
      if (timer) clearInterval(timer);
    };
  }, deps);

  const reload = useCallback(() => run(true), [run]);
  return { ...state, reload };
}

/** Value that settles after `delay` ms without changes. */
export function useDebounced<T>(value: T, delay = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(t);
  }, [value, delay]);
  return settled;
}
