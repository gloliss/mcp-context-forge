/** @type {import('tailwindcss').Config} */

/**
 * ContextForge Admin UI — theme tokens.
 *
 * Scope of this file is deliberately narrow: the neutral (`gray`) and accent
 * (`indigo`) ramps, the sans font stack, and the shape/shadow rhythm.
 *
 * NOT tuned here on purpose: `red` / `green` / `yellow` / `amber` / `orange`.
 * Those families carry error / success / warning meaning in this UI, so
 * retuning them would move semantics, not just looks. `blue` / `purple` /
 * `teal` / `cyan` / `pink` / `rose` / `emerald` are left at Tailwind's values
 * too — their usage is small and semantically mixed.
 *
 * Two invariants for anyone editing the ramps below:
 *
 * 1. ALWAYS extend, never replace. `theme.extend.colors.gray` merges per shade
 *    and keeps the shades you omit; `theme.colors.gray` replaces the whole
 *    family, so a partial ramp would silently stop generating utility classes
 *    for the missing shades ("the style just vanished", with no build error).
 *    That is why both ramps below are complete 50-950.
 * 2. The values are mirrored as CSS custom properties in
 *    `mcpgateway/static/admin.css` (`:root`), which the hand-written rules
 *    (sidebar, chat bubbles, markdown body, scrollbars) consume through
 *    `var()`. `tests/unit/js/themeTokens.test.js` fails if the two drift.
 *    Update both together.
 *
 * The ramps are tuned for a dense operational surface rather than a marketing
 * page: contrast was held at or above the previous Tailwind defaults for every
 * pair the UI actually renders (body text, muted text, borders, dark-mode
 * surfaces, primary buttons, active sidebar), and the dark end is layered so
 * that `gray-800` / `gray-900` / `gray-950` stay distinguishable as surface,
 * page and sunken levels.
 */

module.exports = {
    content: [
        "./mcpgateway/templates/**/*.html",
        "./mcpgateway/admin_ui/**/*.js",
        "./mcpgateway/static/**/*.js",
    ],
    darkMode: "class",
    theme: {
        extend: {
            fontFamily: {
                // Latin faces first and byte-for-byte identical to Tailwind's
                // default `sans` stack, so Latin text keeps rendering in the
                // exact same face as before this change.
                sans: [
                    "ui-sans-serif",
                    "system-ui",
                    "-apple-system",
                    "BlinkMacSystemFont",
                    '"Segoe UI"',
                    "Roboto",
                    '"Helvetica Neue"',
                    "Arial",
                    '"Noto Sans"',
                    // CJK faces. System fonts only — the gateway ships to
                    // offline RHEL, so no webfont may be introduced. Placed
                    // after the Latin faces (Latin glyphs resolve before ever
                    // reaching these) and before the `sans-serif` generic, so
                    // CJK glyphs resolve deterministically instead of by
                    // browser fallback. macOS, Windows, then Linux (the
                    // Source Han Sans / Noto CJK packages ship with the
                    // RHEL/CentOS CJK font groups), then a minimal Linux face.
                    '"PingFang SC"',
                    '"Hiragino Sans GB"',
                    '"Microsoft YaHei"',
                    '"Source Han Sans SC"',
                    '"Noto Sans CJK SC"',
                    '"WenQuanYi Micro Hei"',
                    "sans-serif",
                    // Emoji faces, unchanged from Tailwind's default stack.
                    '"Apple Color Emoji"',
                    '"Segoe UI Emoji"',
                    '"Segoe UI Symbol"',
                    '"Noto Color Emoji"',
                ],
            },
            colors: {
                // Cool, low-chroma neutral. Slightly softer hairlines than
                // Tailwind's `gray` and a smoother dark end, which is where
                // ~7,100 `gray-*` utilities in this UI live.
                gray: {
                    50: "#f9fafb",
                    100: "#f2f4f7",
                    200: "#e4e7ec",
                    300: "#d0d5dd",
                    400: "#9aa4b6",
                    500: "#667085",
                    600: "#475467",
                    700: "#344054",
                    800: "#1d2939",
                    900: "#101828",
                    950: "#0a0e17",
                },
                // Blue-leaning indigo for primary actions, links and the active
                // sidebar item. The 500/600 steps are held dark enough that
                // white-on-indigo stays >= 4.5:1 (AA) for solid buttons, and
                // indigo-600-on-indigo-50 keeps its previous contrast for the
                // active nav state.
                indigo: {
                    50: "#eef4ff",
                    100: "#e0eaff",
                    200: "#c7d7fe",
                    300: "#a4bcfd",
                    400: "#7f91f7",
                    500: "#5566ee",
                    600: "#4547e8",
                    700: "#3538cd",
                    800: "#2d31a6",
                    900: "#2c3178",
                    950: "#1a1d4a",
                },
            },
            // Flatter shape rhythm: the large end is pulled in so panels read as
            // working surfaces instead of oversized cards. `rounded-lg` (the
            // most used step) is intentionally unchanged.
            borderRadius: {
                none: "0",
                sm: "0.1875rem",
                DEFAULT: "0.25rem",
                md: "0.375rem",
                lg: "0.5rem",
                xl: "0.625rem",
                "2xl": "0.75rem",
                "3xl": "1rem",
                full: "9999px",
            },
            // Shadows are tinted with `gray-900` instead of pure black and use
            // smaller blur radii, so elevation stays legible on both the light
            // and dark surfaces without the diffuse "floaty card" look.
            boxShadow: {
                sm: "0 1px 2px 0 rgb(16 24 40 / 5%)",
                DEFAULT: "0 1px 2px 0 rgb(16 24 40 / 6%), 0 1px 3px -1px rgb(16 24 40 / 10%)",
                md: "0 2px 4px -1px rgb(16 24 40 / 6%), 0 4px 8px -2px rgb(16 24 40 / 10%)",
                lg: "0 4px 8px -2px rgb(16 24 40 / 8%), 0 8px 16px -4px rgb(16 24 40 / 10%)",
                xl: "0 8px 16px -4px rgb(16 24 40 / 10%), 0 16px 32px -8px rgb(16 24 40 / 12%)",
                "2xl": "0 20px 40px -12px rgb(16 24 40 / 24%), 0 8px 16px -8px rgb(16 24 40 / 10%)",
                inner: "inset 0 1px 2px 0 rgb(16 24 40 / 6%)",
                none: "none",
            },
            animation: {
                float: "float 6s ease-in-out infinite",
                "pulse-soft": "pulse-soft 2s ease-in-out infinite",
                "slide-up": "slide-up 0.8s ease-out",
                "fade-in": "fade-in 1s ease-out",
            },
            keyframes: {
                float: {
                    "0%, 100%": { transform: "translateY(0px)" },
                    "50%": { transform: "translateY(-20px)" },
                },
                "pulse-soft": {
                    "0%, 100%": { opacity: "1" },
                    "50%": { opacity: "0.8" },
                },
                "slide-up": {
                    "0%": { transform: "translateY(30px)", opacity: "0" },
                    "100%": { transform: "translateY(0)", opacity: "1" },
                },
                "fade-in": {
                    "0%": { opacity: "0" },
                    "100%": { opacity: "1" },
                },
            },
        },
    },
    plugins: [],
};
