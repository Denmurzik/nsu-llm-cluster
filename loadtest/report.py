#!/usr/bin/env python3
"""Summarise a run.py results file: prints the report and writes it as Markdown (results.md).

    python report.py results.jsonl [more.jsonl ...] [--out results.md]

Percentiles use the nearest-rank method. TTFT and latency percentiles describe successful requests only
(a rejected or failed request has no meaningful time to first token); failures are counted separately.
"""

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

SAMPLES = 5
LAG_OVERLOAD_S = 0.2  # p95 event-loop lag of the load generator above which its own timings are not trustworthy


def percentile(values, q: float):
    """Nearest-rank percentile (q in 0..100); None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(math.ceil(q / 100 * len(ordered)), 1)
    return ordered[min(rank, len(ordered)) - 1]


def max_concurrency(records) -> int:
    """Largest number of requests in flight at one moment (a request is in flight from start to end)."""
    events = []
    for r in records:
        events.append((r["start"], 1))
        events.append((r["end"], -1))
    events.sort(key=lambda e: (e[0], e[1]))  # at equal times an end frees its slot before a start takes it
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def _stats(values) -> dict:
    return {"count": len(values), **{f"p{q}": percentile(values, q) for q in (50, 95, 99)},
            "max": max(values) if values else None}


def _is_ok(r) -> bool:
    return r["error"] is None and 200 <= r["status"] < 300


def _group(records, duration) -> dict:
    ok = [r for r in records if _is_ok(r)]
    tokens = sum(r.get("tokens") or 0 for r in ok)
    errors = Counter(r["error"] for r in records if r["error"] not in (None, "http"))
    return {
        "total": len(records),
        "ok": len(ok),
        "status_429": sum(1 for r in records if r["status"] == 429),
        "429_with_retry_after": sum(1 for r in records if r["status"] == 429 and r.get("retry_after")),
        "status_5xx": sum(1 for r in records if 500 <= r["status"] < 600),
        "status_other_4xx": sum(1 for r in records if 400 <= r["status"] < 500 and r["status"] != 429),
        "timeouts": errors.get("timeout", 0),
        "connect_errors": errors.get("connect", 0),
        "protocol_errors": errors.get("protocol", 0) + errors.get("stream_error", 0),
        "other_errors": sum(n for k, n in errors.items() if k not in ("timeout", "connect", "protocol", "stream_error")),
        "statuses": dict(sorted(Counter(r["status"] for r in records).items())),
        "ttft": _stats([r["ttft"] for r in ok if r.get("ttft") is not None]),
        "latency": _stats([r["latency"] for r in ok]),
        "tokens": tokens,
        "tokens_per_s": tokens / duration if duration else 0.0,
        "requests_per_s": len(records) / duration if duration else 0.0,
        "max_concurrency": max_concurrency(records),
    }


def error_samples(records, limit=SAMPLES) -> list[dict]:
    seen: Counter = Counter()
    for r in records:
        if not _is_ok(r):
            seen[(r["status"], r["error"], (r.get("msg") or "")[:120])] += 1
    return [{"status": k[0], "error": k[1], "msg": k[2], "count": n} for k, n in seen.most_common(limit)]


def loop_lag_summary(lags) -> dict | None:
    if not lags:
        return None
    p95 = percentile(lags, 95)
    return {"samples": len(lags), "p50": percentile(lags, 50), "p95": p95, "max": max(lags),
            "overloaded": p95 > LAG_OVERLOAD_S}


def summarize(records) -> dict:
    lags = [r["lag"] for r in records if r["kind"] == "loop_lag"]
    records = [r for r in records if r["kind"] != "loop_lag"]  # lag samples are not requests
    if not records:
        raise ValueError("файл результатов пуст")
    duration = max(r["end"] for r in records) - min(r["start"] for r in records)
    chat = [r for r in records if r["kind"] == "chat"]
    portal = [r for r in records if r["kind"].startswith("portal")]
    return {
        "duration_s": duration,
        "requests": len(records),
        "max_concurrency": max_concurrency(records),
        "loop_lag": loop_lag_summary(lags),
        "chat": _group(chat, duration),
        "portal": _group(portal, duration),
        "portal_by_path": {
            path: _group([r for r in portal if r["kind"] == path], duration)
            for path in sorted({r["kind"] for r in portal})
        },
        "samples": error_samples(records),
    }


def _fmt(value, unit="s") -> str:
    return "-" if value is None else f"{value:.3f} {unit}" if unit else f"{value:.3f}"


def _pct_rows(name: str, s: dict) -> str:
    return (f"| {name} | {s['count']} | {_fmt(s['p50'])} | {_fmt(s['p95'])} | {_fmt(s['p99'])} | {_fmt(s['max'])} |")


def render(summary: dict, source: str) -> str:
    chat, portal = summary["chat"], summary["portal"]
    lag = summary["loop_lag"]
    lines = [
        "# Результаты нагрузочного теста",
        "",
    ]
    if lag and lag["overloaded"]:
        lines += [f"**Внимание: генератор перегружен** (p95 лага цикла событий {lag['p95'] * 1000:.0f} мс, порог "
                  f"{LAG_OVERLOAD_S * 1000:.0f} мс): измеренные TTFT и задержки завышены самим генератором. "
                  "Разделите нагрузку между процессами или хостами (`run.py --shard I/N`) и повторите прогон.", ""]
    lines += [
        f"Источник: `{source}`. Длительность {summary['duration_s']:.1f} с, всего запросов {summary['requests']}, "
        f"максимум одновременных запросов {summary['max_concurrency']}.",
        "",
        "## Генератор нагрузки",
        "",
        ("Лаг цикла событий (насколько позже срабатывает таймер): "
         + (f"p50 {lag['p50'] * 1000:.0f} мс, p95 {lag['p95'] * 1000:.0f} мс, макс. {lag['max'] * 1000:.0f} мс, "
            f"замеров {lag['samples']}; "
            + ("генератор перегружен." if lag["overloaded"] else "генератор не перегружен.")
            if lag else "замеров нет (результаты старого формата).")),
        "",
        "## Запросы к моделям (chat)",
        "",
        "| Показатель | Значение |",
        "| --- | --- |",
        f"| всего | {chat['total']} |",
        f"| успешно | {chat['ok']} |",
        f"| 429 | {chat['status_429']} (с Retry-After: {chat['429_with_retry_after']}) |",
        f"| 5xx | {chat['status_5xx']} |",
        f"| прочие 4xx | {chat['status_other_4xx']} |",
        f"| таймауты | {chat['timeouts']} |",
        f"| ошибки соединения | {chat['connect_errors']} |",
        f"| ошибки протокола и потока | {chat['protocol_errors']} |",
        f"| прочие ошибки | {chat['other_errors']} |",
        f"| запросов в секунду | {chat['requests_per_s']:.1f} |",
        f"| токенов (успешные) | {chat['tokens']} |",
        f"| токенов в секунду | {chat['tokens_per_s']:.1f} |",
        f"| одновременно в полёте (макс.) | {chat['max_concurrency']} |",
        f"| коды ответов | {', '.join(f'{k}: {v}' for k, v in chat['statuses'].items()) or '-'} |",
        "",
        "Перцентили по успешным запросам (метод ближайшего ранга):",
        "",
        "| Метрика | n | p50 | p95 | p99 | max |",
        "| --- | --- | --- | --- | --- | --- |",
        _pct_rows("TTFT (первый SSE-чанк)", chat["ttft"]),
        _pct_rows("Задержка ответа целиком", chat["latency"]),
    ]
    if portal["total"]:
        lines += ["", "## Трафик портала", "",
                  "| Путь | всего | успешно | не 2xx | p50 | p95 | p99 |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for path, g in summary["portal_by_path"].items():
            lat = g["latency"]
            lines.append(f"| `{path.removeprefix('portal:')}` | {g['total']} | {g['ok']} | {g['total'] - g['ok']} | "
                         f"{_fmt(lat['p50'])} | {_fmt(lat['p95'])} | {_fmt(lat['p99'])} |")
    lines += ["", "## Примеры ошибок", ""]
    if summary["samples"]:
        lines += ["| Код | Класс | Сообщение | Число |", "| --- | --- | --- | --- |"]
        for s in summary["samples"]:
            lines.append(f"| {s['status']} | {s['error']} | {s['msg'].replace('|', '/') or '-'} | {s['count']} |")
    else:
        lines.append("Ошибок нет.")
    return "\n".join(lines) + "\n"


def load_records(paths) -> list[dict]:
    """Records of one or several results files (shards of one run are merged; host clocks must agree)."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    return [json.loads(line) for path in paths
            for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Отчёт по результатам run.py")
    parser.add_argument("results", nargs="+", help="results.jsonl; несколько файлов (шарды) объединяются")
    parser.add_argument("--out", help="куда записать Markdown (по умолчанию results.md рядом с входным файлом)")
    args = parser.parse_args(argv)
    try:
        summary = summarize(load_records(args.results))
    except (OSError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    text = render(summary, ", ".join(Path(p).name for p in args.results))
    out = Path(args.out) if args.out else Path(args.results[0]).with_name("results.md")
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"Записано: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
