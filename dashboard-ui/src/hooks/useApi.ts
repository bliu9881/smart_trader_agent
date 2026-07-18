import { useEffect, useState } from 'react';

export function useApi<T>(fetcher: () => Promise<T>, intervalMs = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let mounted = true;

    const poll = async () => {
      try {
        const result = await fetcher();
        if (mounted) {
          setData(result);
          setError(null);
        }
      } catch (e: any) {
        if (mounted) setError(e.message);
      } finally {
        if (mounted) setLoading(false);
      }
    };

    poll();
    const id = setInterval(poll, intervalMs);
    return () => { mounted = false; clearInterval(id); };
  }, [intervalMs]);

  return { data, error, loading };
}
