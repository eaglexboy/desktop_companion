// Pure helpers for step-function time series: [[ts, value], ...] sorted by ts.

// The value in effect at time t: the last point at or before it (null before the first).
export function valueAt(points, t) {
  let lo = 0,
    hi = points.length - 1,
    answer = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= t) {
      answer = points[mid][1];
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return answer;
}
