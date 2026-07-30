import { describe, expect, it } from "vitest";

import { formatDuration } from "./format";

describe("formatDuration", () => {
  it("formats sub-minute durations", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(59)).toBe("0:59");
  });

  it("formats minutes and seconds", () => {
    expect(formatDuration(60)).toBe("1:00");
    expect(formatDuration(605)).toBe("10:05");
  });

  it("formats hours with zero-padded minutes", () => {
    expect(formatDuration(3600)).toBe("1:00:00");
    expect(formatDuration(3930)).toBe("1:05:30");
  });

  it("clamps negative and fractional input", () => {
    expect(formatDuration(-5)).toBe("0:00");
    expect(formatDuration(61.9)).toBe("1:01");
  });
});
