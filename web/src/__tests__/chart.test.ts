import { PLOT_H, PLOT_W, linePath, nearest, niceMax } from "../lib/chart";

describe("niceMax", () => {
  it("rounds up to 1, 2, 5 or 10 times a power of ten", () => {
    expect([0.4, 1, 1.5, 2, 3, 5, 6, 63, 380, 4200].map(niceMax)).toEqual([0.5, 1, 2, 2, 5, 5, 10, 100, 500, 5000]);
  });
  it("has a floor of one for nothing or nonsense", () => {
    expect([0, -3, NaN].map(niceMax)).toEqual([1, 1, 1]);
  });
});

describe("linePath", () => {
  const MIN = 60_000;
  const pt = (i: number, v: number | null) => ({ t: i * MIN, v });

  it("joins consecutive points, the top of the axis is the top of the plot", () => {
    const d = linePath([pt(0, 0), pt(5, 50), pt(10, 100)], 0, 10 * MIN, 100, 6 * MIN);
    expect(d).toBe(`M0.0 ${PLOT_H.toFixed(1)}L${(PLOT_W / 2).toFixed(1)} ${(PLOT_H / 2).toFixed(1)}L${PLOT_W.toFixed(1)} 0.0`);
  });

  it("breaks the line where the samples are further apart than the gap", () => {
    const d = linePath([pt(0, 0), pt(5, 50), pt(10, 100)], 0, 10 * MIN, 100, 2 * MIN);
    expect(d.match(/M/g)).toHaveLength(3); // the gap is five minutes: three lone dots
  });

  it("breaks the line at a null", () => {
    const d = linePath([pt(0, 1), pt(1, 2), pt(2, null), pt(3, 3), pt(4, 4), pt(20, 5), pt(21, 6)], 0, 21 * MIN, 10, 2 * MIN);
    expect(d.match(/M/g)).toHaveLength(3);
  });

  it("draws a lone sample as a dot", () => {
    expect(linePath([pt(5, 3)], 0, 10 * MIN, 10, 2 * MIN)).toBe("M300.0 98.0h0");
  });

  it("clamps values to the axis and skips points outside the period", () => {
    expect(linePath([pt(0, 500), pt(1, 5)], 0, 10 * MIN, 10, 2 * MIN)).toBe(
      `M0.0 0.0L${(PLOT_W / 10).toFixed(1)} ${(PLOT_H / 2).toFixed(1)}`,
    );
    expect(linePath([pt(-5, 1), pt(50, 1)], 0, 10 * MIN, 10, 2 * MIN)).toBe("");
  });

  it("is empty without values", () => {
    expect(linePath([], 0, MIN, 10, MIN)).toBe("");
    expect(linePath([pt(0, null)], 0, MIN, 10, MIN)).toBe("");
  });
});

describe("nearest", () => {
  it("finds the closest time or -1", () => {
    expect(nearest([10, 20, 40], 24)).toBe(1);
    expect(nearest([10, 20, 40], 33)).toBe(2);
    expect(nearest([], 5)).toBe(-1);
  });
});
