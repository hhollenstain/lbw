import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api";
import { workspacePath } from "../../page";

export type UsageScope = "org" | "project" | "me";

type Dimension = { key: string; label: string; otel?: string };
type Measure = { key: string; label: string; unit: string; otel?: string };
type Dataset = {
  key: string;
  label: string;
  note: string;
  dimensions: Dimension[];
  measures: Measure[];
};
type Registry = {
  datasets: Dataset[];
  max_days: number;
  default_days: number;
  can_see_org: boolean;
};
type Report = {
  totals: Record<string, number>;
  rows: Record<string, string | number>[];
  series: Record<string, string | number>[];
  labels: Record<string, Record<string, string>>;
  since: string;
  until: string;
  cached: boolean;
};

const RANGES = [7, 30, 90, 365];

/** Usage for one scope. Everything it offers comes from /api/usage/meta, so a new
 * breakdown added on the server shows up here without a change. */
export function UsageDashboard(props: { scope: UsageScope }) {
  const [meta, setMeta] = useState<Registry | null>(null);
  const [dataset, setDataset] = useState("tokens");
  const [groupBy, setGroupBy] = useState("model");
  const [measure, setMeasure] = useState("tokens");
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);
  const [shown, setShown] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Nothing is fetched until the dashboard is on screen.
    if (root.current && !root.current.closest("[hidden]")) setShown(true);
    function onShow(event: Event) {
      const detail = (event as CustomEvent<{ scope?: string }>).detail || {};
      if (detail.scope && detail.scope !== props.scope) return;
      setShown(true);
      setTick((value) => value + 1);
    }
    document.addEventListener("lbw-usage-show", onShow);
    return () => document.removeEventListener("lbw-usage-show", onShow);
  }, [props.scope]);

  useEffect(() => {
    if (!shown || meta) return;
    api<Registry>("/api/usage/meta")
      .then(setMeta)
      .catch((err) => setError(err instanceof Error ? err.message : "usage is unavailable"));
  }, [shown, meta]);

  const allowed = props.scope !== "org" || Boolean(meta?.can_see_org);

  const current = meta?.datasets.find((item) => item.key === dataset) || meta?.datasets[0];
  const breakdowns = (current?.dimensions || []).filter(
    (item) =>
      item.key !== "day" &&
      !(props.scope === "me" && item.key === "person") &&
      !(props.scope === "project" && item.key === "project"),
  );
  const chosenMeasure =
    current?.measures.find((item) => item.key === measure) || current?.measures[0];
  const chosenGroup = breakdowns.find((item) => item.key === groupBy) || breakdowns[0];

  const query = useMemo(() => {
    if (!current || !chosenMeasure || !allowed) return "";
    const until = new Date();
    const since = new Date(until.getTime() - (days - 1) * 86_400_000);
    const params = new URLSearchParams({
      scope: props.scope,
      dataset: current.key,
      measures: current.measures.map((item) => item.key).join(","),
      since: isoDay(since),
      until: isoDay(until),
    });
    if (chosenGroup) params.set("group_by", chosenGroup.key);
    if (props.scope === "project") params.set("path", workspacePath());
    return params.toString();
  }, [current, chosenMeasure, chosenGroup, days, props.scope, tick, allowed]);

  const load = useCallback(() => {
    if (!query) return;
    api<Report>(`/api/usage?${query}`)
      .then((body) => {
        setReport(body);
        setError("");
      })
      .catch((err) => setError(err instanceof Error ? err.message : "usage is unavailable"));
  }, [query]);

  useEffect(load, [load]);

  if (!meta || !current || !chosenMeasure) {
    return (
      <div ref={root}>
        <p className="hint">{error || "Loading usage…"}</p>
      </div>
    );
  }
  if (!allowed) {
    return (
      <div ref={root}>
        <p className="hint">Owners, admins, and people with the billing duty see this.</p>
      </div>
    );
  }

  const tiles = current.measures.slice(0, 4);
  const series = fillDays(report?.series || [], days, chosenMeasure.key);
  const rows = report?.rows || [];
  const top = rows.reduce((most, row) => Math.max(most, Number(row[chosenMeasure.key]) || 0), 0);
  const total = Number(report?.totals[chosenMeasure.key]) || 0;
  const id = `usage-${props.scope}`;

  return (
    <div className="usage-dashboard" id={id} ref={root}>
      <div className="usage-filters" role="group" aria-label="Usage report">
        <label htmlFor={`${id}-dataset`}>Show</label>
        <select
          id={`${id}-dataset`}
          value={current.key}
          onChange={(event) => {
            const next = meta.datasets.find((item) => item.key === event.target.value);
            setDataset(event.target.value);
            if (next) setMeasure(next.measures[0]?.key || "");
          }}
        >
          {meta.datasets.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <label htmlFor={`${id}-measure`}>Measure</label>
        <select
          id={`${id}-measure`}
          value={chosenMeasure.key}
          onChange={(event) => setMeasure(event.target.value)}
        >
          {current.measures.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <label htmlFor={`${id}-group`}>By</label>
        <select
          id={`${id}-group`}
          value={chosenGroup?.key || ""}
          onChange={(event) => setGroupBy(event.target.value)}
        >
          {breakdowns.map((item) => (
            <option key={item.key} value={item.key}>
              {item.label}
            </option>
          ))}
        </select>
        <label htmlFor={`${id}-days`}>Range</label>
        <select
          id={`${id}-days`}
          value={String(days)}
          onChange={(event) => setDays(Number(event.target.value))}
        >
          {RANGES.filter((value) => value <= meta.max_days).map((value) => (
            <option key={value} value={value}>
              Last {value} days
            </option>
          ))}
        </select>
        <a className="secondary usage-export" href={`/api/usage?${query}&format=csv`} download>
          Export CSV
        </a>
      </div>
      {current.note ? <p className="hint">{current.note}</p> : null}
      {error ? <p className="modal-error">{error}</p> : null}
      <div className="usage-tiles">
        {tiles.map((item) => (
          <div className="usage-tile" key={item.key}>
            <span className="usage-tile-label">{item.label}</span>
            <strong className="usage-tile-value">
              {format(Number(report?.totals[item.key]) || 0, item.unit)}
            </strong>
          </div>
        ))}
      </div>
      <h3 className="rule-heading">{chosenMeasure.label} by day</h3>
      <DailyBars series={series} measure={chosenMeasure} />
      {chosenGroup ? (
        <>
          <h3 className="rule-heading">
            {chosenMeasure.label} by {chosenGroup.label.toLowerCase()}
          </h3>
          {rows.length ? (
            <table className="usage-breakdown">
              <thead>
                <tr>
                  <th scope="col">{chosenGroup.label}</th>
                  <th scope="col" className="usage-bar-cell">
                    <span className="sr-only">Share</span>
                  </th>
                  <th scope="col" className="usage-number">
                    {chosenMeasure.label}
                  </th>
                  <th scope="col" className="usage-number">
                    Share
                  </th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const raw = String(row[chosenGroup.key] || "");
                  const name = report?.labels[chosenGroup.key]?.[raw] || raw || "(none)";
                  const value = Number(row[chosenMeasure.key]) || 0;
                  const width = top ? Math.max((value / top) * 100, value ? 1 : 0) : 0;
                  return (
                    <tr key={raw || "(none)"}>
                      <th scope="row" title={raw && name !== raw ? raw : undefined}>
                        {name}
                      </th>
                      <td className="usage-bar-cell" aria-hidden="true">
                        <span className="usage-bar" style={{ width: `${width}%` }} />
                      </td>
                      <td className="usage-number">{format(value, chosenMeasure.unit)}</td>
                      <td className="usage-number">
                        {total ? `${Math.round((value / total) * 100)}%` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <p className="hint">Nothing recorded in this range yet.</p>
          )}
        </>
      ) : null}
      {report?.cached ? (
        <p className="hint">Figures can be up to a minute old.</p>
      ) : null}
    </div>
  );
}

/** One measure per day as bars, with a tooltip per bar and a total line in the title. */
function DailyBars(props: { series: { day: string; value: number }[]; measure: Measure }) {
  const [hover, setHover] = useState<number | null>(null);
  const { series, measure } = props;
  const width = 640;
  const height = 160;
  const top = 12;
  const bottom = 22;
  const most = series.reduce((max, item) => Math.max(max, item.value), 0);
  const step = width / Math.max(series.length, 1);
  const bar = Math.max(Math.min(step - 2, 18), 1);
  const plot = height - top - bottom;
  const shown = hover === null ? null : series[hover];
  if (!series.some((item) => item.value)) {
    return <p className="hint">Nothing recorded in this range yet.</p>;
  }
  return (
    <div className="usage-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`${measure.label} per day, highest ${format(most, measure.unit)}`}
        onMouseLeave={() => setHover(null)}
      >
        <line
          className="usage-axis"
          x1={0}
          x2={width}
          y1={height - bottom + 0.5}
          y2={height - bottom + 0.5}
        />
        {series.map((item, index) => {
          const tall = most ? (item.value / most) * plot : 0;
          const x = index * step + (step - bar) / 2;
          return (
            <g key={item.day} onMouseEnter={() => setHover(index)}>
              <rect className="usage-hit" x={index * step} y={top} width={step} height={plot} />
              {tall > 0 ? (
                <path
                  className={hover === index ? "usage-mark usage-mark-on" : "usage-mark"}
                  d={roundedTop(x, height - bottom, bar, Math.max(tall, 2), Math.min(4, bar / 2))}
                />
              ) : null}
            </g>
          );
        })}
        <text className="usage-tick" x={0} y={height - 6}>
          {shortDay(series[0]?.day || "")}
        </text>
        <text className="usage-tick" x={width} y={height - 6} textAnchor="end">
          {shortDay(series[series.length - 1]?.day || "")}
        </text>
        <text className="usage-tick" x={width} y={top - 2} textAnchor="end">
          {format(most, measure.unit)}
        </text>
      </svg>
      {shown ? (
        <div
          className="usage-tooltip"
          style={{ left: `${(((hover || 0) + 0.5) / series.length) * 100}%` }}
          role="status"
        >
          <span>{shortDay(shown.day)}</span>
          <strong>{format(shown.value, measure.unit)}</strong>
        </div>
      ) : null}
    </div>
  );
}

function roundedTop(x: number, base: number, w: number, h: number, r: number) {
  const y = base - h;
  const radius = Math.min(r, h);
  return [
    `M${x},${base}`,
    `V${y + radius}`,
    `Q${x},${y} ${x + radius},${y}`,
    `H${x + w - radius}`,
    `Q${x + w},${y} ${x + w},${y + radius}`,
    `V${base}`,
    "Z",
  ].join(" ");
}

function fillDays(series: Record<string, string | number>[], days: number, key: string) {
  const byDay = new Map(series.map((item) => [String(item.day), Number(item[key]) || 0]));
  const out: { day: string; value: number }[] = [];
  const end = new Date();
  for (let index = days - 1; index >= 0; index -= 1) {
    const day = isoDay(new Date(end.getTime() - index * 86_400_000));
    out.push({ day, value: byDay.get(day) || 0 });
  }
  return out;
}

function isoDay(value: Date) {
  return value.toISOString().slice(0, 10);
}

function shortDay(day: string) {
  if (!day) return "";
  const parsed = new Date(`${day}T00:00:00Z`);
  return parsed.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function format(value: number, unit: string) {
  if (unit === "usd_micros") {
    const dollars = value / 1_000_000;
    return dollars.toLocaleString(undefined, {
      style: "currency",
      currency: "USD",
      maximumFractionDigits: dollars < 10 ? 2 : 0,
    });
  }
  if (unit === "ms") {
    const seconds = value / 1000;
    if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
    const minutes = seconds / 60;
    if (minutes < 90) return `${Math.round(minutes)}m`;
    return `${(minutes / 60).toFixed(1)}h`;
  }
  return new Intl.NumberFormat(undefined, {
    notation: value >= 100_000 ? "compact" : "standard",
    maximumFractionDigits: 1,
  }).format(value);
}
