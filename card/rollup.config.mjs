import nodeResolve from "@rollup/plugin-node-resolve";
import terser from "@rollup/plugin-terser";
import typescript from "@rollup/plugin-typescript";

// The bundle is emitted INTO the integration (not dist/, which is gitignored)
// and committed, so a HACS install of custom_components/ delivers the card
// in the same install (AD-12).
export default {
  input: "src/ha-irrigation-timeline-card.ts",
  output: {
    file: "../custom_components/ha_irrigation_controller/frontend/ha-irrigation-timeline-card.js",
    format: "es",
    sourcemap: false,
  },
  plugins: [
    nodeResolve(),
    // tsconfig includes the test files so `tsc --noEmit` checks them; they must
    // not reach the bundle.
    typescript({ exclude: ["src/**/*.test.ts"] }),
    // ES2022 output — no ES5 transpilation (it breaks Lit 3 native classes).
    terser({ ecma: 2022, format: { comments: false } }),
  ],
};
