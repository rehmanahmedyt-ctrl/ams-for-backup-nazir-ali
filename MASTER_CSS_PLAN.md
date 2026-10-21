# Master CSS Plan — Visual Perfection for Every App Page

**Status:** Plan only (docs) — no code changes in this PR. This is the app-wide companion to `DIRECT_SALES_CSS_PLAN.md`.
**Scope:** all **120 templates** / every routed page. **CSS-only**: changes live in `static/theme.css`, `static/ui.css`, `static/pan_view.css`, `templates/layout.html`'s `<style>`, and per-page `<style>` blocks. No markup restructure, no JS, no Python, no route changes.

**Confirmed decisions**
- **CSS-only.** Where the ideal fix is markup (e.g. a table column restructure), the plan records the best CSS-only approximation and notes the markup fix as a backlog item — it is not executed here.
- **Tokens declared once** (in `theme.css`); `ui.css` and page styles only consume them.
- **Control scale: 40 / 34 / 30 px** (default / compact / mobile) — matches the Direct Sales plan.
- **Both themes are first-class:** every phase is verified in dark **and** light mode.
- **Direct Sales page** stays governed by `DIRECT_SALES_CSS_PLAN.md`; this plan owns its *shared* fixes (tokens, chevron, pagination, table row bg) exactly as that plan's "shared fixes allowed" clause permits.

**Files in scope (the whole cascade)**

| File | Size / weight | What it contributes |
|---|---|---|
| `static/theme.css` | 593 lines, **163 `!important`** | Global tokens, `form-control/select/btn` sizing, tables, light-mode overrides, combobox, flatpickr |
| `static/ui.css` | 1,374 lines, **84 `!important`** | `--ui-*` design system: cards, KPI tiles, tables, `.ui-form`/`.ui-v2`, `.ds-*` page chrome, role modal |
| `static/pan_view.css` | 160 lines | Pan/fit overlay for wide table wrappers (global) |
| `templates/layout.html` | 290-line inline `<style>` | Shell: sidebar, topbar, page wrapper, `.nav-link`, mobile sidebar |
| 57 templates with inline `<style>` | 502 `!important` total | Page chrome: `direct_sales.html` (3,059 l), `cash_flow.html` (1,873 l), `client_ledger.html` (917 l), `payments.html` (870 l), `bookings.html` (689 l), `view_bill.html` (651 l), `material_returns.html` (547 l), `tracking.html` (527 l), `pending_bills.html` (524 l), `supplier_ledger.html` (501 l), `financial_ledger.html` (470 l), `import_export_new.html` (422 l), `tenants.html` (384 l), `accounts/*` (12 blocks) … |
| `templates/accounts/_acc_styles.html` | 62 lines | A **fourth** design language: gradient header + gradient `.kpi-card` tiles |

**Utility-soup inventory (markup classes re-skinned by CSS):** `bg-dark` × **690** (67 files), `text-white-50` × **1,075**, `border-secondary` × **881**, hard-coded hex colours in templates × **685**. Total `!important` in the cascade ≈ **749**.

**Method:** static reading of the full cascade (Bootstrap 5.3 → theme.css → ui.css → pan_view.css → layout inline → page inline), with file:line anchors. No browser is available in this environment, so **Phase 0 is a screenshot baseline** captured from the running app before any edit.

---

## 1. Executive summary — the 12 app-wide problems

| # | Severity | Problem | Effect on the user |
|---|---|---|---|
| 1 | **P0** | **Two token systems fight.** `theme.css:24-25` sets `--ui-field-h:38/32`, `ui.css:45-46` re-declares `40/34`; mobile `34/30` exists only in `theme.css:299-300`. | Field heights differ page to page and never respond to phones consistently. |
| 2 | **P0** | **Select chevrons wiped** by the `background` shorthand in `ui.css:296` (`.ui-form`), `ui.css:486-490` (`.ui-v2`, 7 templates), `ui.css:1278` (role form), plus the sale sheets. Bootstrap's chevron is `background-image` — the shorthand resets it to `none`. | Most selects in the app have **no dropdown arrow**; users don't know they open. |
| 3 | **P0** | **Row hover is dead on every plain `.table`**: `theme.css:98-100` paints `background: var(--table-row) !important` on `tbody tr`, beating every hover rule (`ui.css:361-363` etc.). | 30+ list/ledger pages have no pointer feedback; rows read as one flat slab. |
| 4 | **P1** | **Pagination active state is invisible**: `theme.css:371-375` forces `!important` colours on every `.page-link`, defeating `bg-warning`/`active` utilities. | On every paginated page the current page looks like any other. |
| 5 | **P1** | **Three input palettes for the same job**: `--input-bg` (theme filters), `--ui-surface-2` (`.ui-form`/sheets), `bg-dark` → `--surface-2` (67 templates). Light mode shows white vs blue-grey vs slate side by side. | One page shows 2–3 different greys for identical controls. |
| 6 | **P1** | **Two KPI systems**: `.ui-kpi` (token tiles, 11 colours, `ui.css`) vs `accounts/_acc_styles.html` `.kpi-card` (hard gradients `#10b981→#047857` etc., `color:#fff !important`). | Dashboards that mix the two (or sibling pages) look like different products. |
| 7 | **P1** | **Filter-action rows stack vertically**: `theme.css:273-277` forces every `.ui-helper-form .row.form-grid-top > [class*="col-"]` to `flex-direction: column`; Apply/Reset drop under the labels; 13-column `lg` grids (12 available) wrap to a second line. | Every filter bar wastes a line and misaligns its buttons. |
| 8 | **P1** | **Combobox popup is peach in every theme**: `theme.css:387-398` forces `#ffe7c2` / `#dcfce7` with `!important`; the theme-aware version (`ui.css:458-480`) is scoped to `.ui-v2`/`.ui-combobox-v2` only. | Client/Material pickers open a cream box over dark modals. |
| 9 | **P2** | **Type & radius chaos**: 32+ `font-size` values (`0.62rem` labels to `5.2rem` hero numerals) and 8 radii (8/9/10/12/14/16/20/999px + Bootstrap's 6) across pages. | No vertical rhythm; the same control looks different on every screen. |
| 10 | **P2** | **Light mode is held together by `!important` accidents** (163 in `theme.css`, plus `[style*="background: #0f172a"]` attribute hacks at `theme.css:400-460` re-colouring inline styles by string match). | Light mode silently breaks whenever a template edits one hex colour. |
| 11 | **P2** | **Per-page responsive drift**: `direct_sales` uses 1199/991/767/575 cuts, `bookings` uses its own 38/36 px heights (`bookings.html:12-22`), `payments.html` a 575-only block, accounts uses none — while `pan_view.css` hijacks horizontal overflow on wide tables app-wide. | Tablet widths look accidental; some pages scroll, some clip, some pan. |
| 12 | **P2** | **Focus rings inconsistent or removed** (e.g. `box-shadow:none !important` in the sale date shell; 15 %-alpha rings elsewhere; several `outline:none`). | Keyboard users lose their place on the busiest forms. |

**Verdict:** the app does not need 120 page-by-page re-skins. It needs **one token contract, one set of component rules (inputs, buttons, tables, filters, KPIs, modals), and a per-page sweep that deletes duplicates** — applied in 13 small, reversible phases below. Roughly **60 % of the visual win is Phase 1–2 alone** (shared fixes that touch every page at once).

---

## 2. How the CSS is layered (why every page drifts)

```
bootstrap.min.css 5.3
  └─ theme.css          --surface-*, --accent, --input-bg; --ui-field-h:38/32;
       │                  .btn/.form-control global !important; .table tbody tr bg !important;
       │                  pagination !important; combobox peach; attribute-selector hacks
       └─ ui.css         --ui-* tokens (re-declares --ui-field-h:40/34); .ui-card/.ui-kpi/
       │                  .ui-table/.ui-form/.ui-v2; .ds-* chrome; role modal; mobile cards
       └─ pan_view.css   .ams-pan-* wide-table panning + fit-to-width
            └─ layout.html <style>   shell: sidebar/topbar/page gutters/nav-link
                 └─ page <style> ×57  page chrome, sometimes a private component kit
                      └─ accounts/_acc_styles.html   gradient KPI/header kit (accounts only)
```

Consequences already proven on the Direct Sales page (see that plan's §2) hold **everywhere**:

1. **Tokens are declared twice.** Anything that uses `var(--ui-field-h)` gets 40 px on pages with `ui.css` effects and 38 px wherever `theme.css` alone decides (its own `.form-control` min-height rule at `theme.css:157-161` reads the *final* cascade value 40 — but the mobile override at `theme.css:299-300` has no `ui.css` counterpart, so phone sizes depend on which rule wins per component).
2. **`!important` is doing the theming.** ~749 of them. Rules are written to out-`!important` the previous layer, which is exactly why hover (G3), pagination (G4) and chevrons (G2) are silently lost.
3. **Each page re-invents chrome** in its own `<style>` block — 57 times — so a fix must be repeated 2–5× and the copies drift (already true inside Direct Sales alone).

---

## 3. Cross-cutting findings (G1–G14)

### G1 — Token duplication (P0)
- `theme.css:24-25`: `--ui-field-h:38px; --ui-field-h-sm:32px` (plus `--ui-gap-*`, `--ui-label-h`).
- `ui.css:45-46`: `--ui-field-h:40px; --ui-field-h-sm:34px` — later, so it wins everywhere loaded.
- `theme.css:299-300` (mobile): `34/30` — no `ui.css` mirror.
- Meanwhile `bookings.html:12-22` hard-codes `min-height:38px/36px` inside `.booking-modal-dialog`, and `direct_sales.html:227-232` hard-codes `36px` — two private workarounds for the missing single scale.

### G2 — Select chevron destroyed (P0)
`background` shorthand (which resets `background-image`) at: `ui.css:296` (`.ui-form .form-select`), `ui.css:486-490` (`.ui-v2 .form-select` — 7 templates), `ui.css:1278` (`.role-form-field .form-select`), `direct_sales.html:234-237` and `_direct_sale_edit_modal.html` equivalents. Even where the chevron survives (`theme.css:151-155` correctly uses `background-color`), Bootstrap's default arrow is `#343a40` — invisible on dark inputs. Needs a **theme-aware chevron image**, not just a colour swap.

### G3 — Dead table hover (P0)
`theme.css:98-100` `.table tbody tr { background: var(--table-row) !important }` defeats `ui.css:361-363` (`.ui-table` hover), `ui.css:684-686`, `ui.css:889-897` (mobile card tint) and every inline hover. Affects every list/ledger page that uses plain `.table` (the majority).

### G4 — Pagination active state invisible (P1)
`theme.css:371-375` sets `background-color/border-color/color … !important` on all `.page-link`. Markup using `active` + `bg-warning` (or `.page-item.active`) cannot win. Fix once, globally: add an `.active`/`[aria-current]` rule *after* it with equal `!important`, accent colours.

### G5 — Three input palettes (P1)
- Filter inputs: `--input-bg` / `--input-border` (`theme.css:151-155`).
- `.ui-form` / `.ui-v2` / sale sheets: `--ui-surface-2` / `--ui-border-strong`.
- Utility soup: `bg-dark text-white border-secondary` (690 + 881 occurrences) re-mapped by `theme.css:145-149` + attribute hacks.
Light mode therefore shows white, `#eef2f9` and `#e9eef5` inputs on one screen. Contract: **one input surface token** (accept two *purposes*: input vs card, never three ad-hoc greys).

### G6 — Two KPI systems (P1)
`ui.css` `.ui-kpi` (tokens, `.ui-kpi-value 2.1rem`, colour words amber/green/red/…) vs `accounts/_acc_styles.html` `.kpi-card` (hard-coded gradients, `color:#fff !important`, 4.5rem watermark icon, `border-radius:14px`). Accounts also has its own `.acc-page-header` gradient vs `.ui-page-header`. Unify **look and metrics** via CSS (keep both class names; make `.kpi-card` consume the same tokens/radii/type sizes).

### G7 — Filter bars (P1)
- `theme.css:273-277`: every `form-grid-top` child column forced `flex-direction: column` — Apply/Reset stack vertically (all pages using `.ui-helper-form`).
- `theme.css:283-287`: `.form-action-row`/`.ui-actions-col` also forced `column`.
- Direct Sales: 13 `col-lg-*` (over 12) — same pattern appears in payments/clients/ledgers where filter rows sit at the 12-column edge.
- Contract: 12-column filter grid + **one horizontal actions row** (`flex-direction:row` override scoped to `.filter-actions`, or fix the `theme.css` rule with `:where()` lowering).

### G8 — Combobox peach popup (P1)
`theme.css:385-398`: `.combobox-list/#ffe7c2 !important`, `.combobox-item` cream/green hover — global. Theme-aware rules exist only under `.ui-v2` (`ui.css:458-480`). Every page whose pickers use the plain combobox (clients, direct sales, bookings, accounts forms) shows the cream popup in **both** themes. Fix globally: token-driven list/item colours + hover using `color-mix` accent tints.

### G9 — Attribute-selector theme hacks (P2)
`theme.css:400-460` re-colours inline styles by matching `[style*="background: #0f172a"]` and friends (spacing-sensitive, fragile). Keep until templates stop hard-coding hexes (685 of them); then delete in the Phase 12 sweep. Never add new ones.

### G10 — Type scale chaos (P2)
`ui.css` alone declares 25+ sizes (0.65–5.2rem); templates add 0.62/0.68/0.71/0.73/0.74/0.76rem micro-labels (≈10 px on phones). Contract (§6.2): one 8-step scale, one label style (`.ui-label`, 0.78rem / tracking .04em), one hint style (0.72rem), page numerals via `clamp()`.

### G11 — Radius chaos (P2)
8 px (`btn-sm`), 9 px (`.btn`), 10 px (`--ui-radius`), 12 px (mobile cards), 14 px (`.kpi-card`/`.acc-section-card`), 16 px (`--ui-radius-lg`, `.ui-card`, `.acc-page-header`), 20 px (`.login-card`), 999 px pills. Contract: **6 / 10 / 16 / pill** (keep 9→10 and 12/14→16 via token remap in CSS).

### G12 — Focus states (P2)
Mixed: 35 %-alpha accent rings (`ui.css`), 15 %-alpha amber (`direct_sales.html:242`), `box-shadow:none !important` (sale date shell), bare `outline:none` in a few inline blocks. Contract: one `:focus-visible` rule per control family (§6.8) and **delete every `box-shadow:none`/`outline:none` that hides it**.

### G13 — Light mode by accident (P2)
163 `!important` in `theme.css`, several light-only blocks targeting dead classes (e.g. `theme.css:486-540` `.summary-material`, `.field-label`, `.item-total-*` — confirmed gone from the live Direct Sales markup). Contract: one `[data-theme="light"]` block **per component**, written against classes that exist; sweep deletes dead ones.

### G14 — Responsive drift (P2)
Breakpoint sets used: 1199.98/991.98/767.98/575.98 (direct sales), 991/575 (payments mobile block), ad-hoc 575 (login), none (accounts). `pan_view.css` adds a *fourth* behaviour on wide tables (drag-pan + hidden scrollbar + Fit button). Contract: Bootstrap's four cuts only; mobile card-stack (`ui.css:880-940`) extended as the shared `.table` fallback; pan_view remains opt-in for `.table-responsive` wrappers over 1200 px content.

---

## 4. Page inventory ("all pages" = these families)

| Family | Pages / templates | Styling today | Primary visual targets |
|---|---|---|---|
| **A. Shell & auth** | `layout.html` (sidebar/topbar), `login.html`, `limited_access.html` | layout inline + tiny page blocks | Nav rhythm, active state, login card in both themes, topbar height contract |
| **B. Dashboards** | `index.html`, `admin_dashboard.html`, `accounts/dashboard.html` | `.ui-v2`/`.ui-kpi` vs `acc` gradients | One KPI look, one page-header look, equal card gaps |
| **C. Transaction screens** | `direct_sales.html` (+`_direct_sale_edit_modal`), `bookings.html` (+`_booking_edit_modal`), `grn.html`, `grn_wizard.html`, `material_returns.html`, `dispatching.html`, `receiving.html`, `daily_transactions.html`, `mixed_transactions.html` | Large inline kits (500–3,000 l) with private item-grids | Equal field heights in every item row; shared sheet/modal chrome; chevrons; filters (Direct Sales defers to its own plan) |
| **D. List / master pages** | `clients.html`, `suppliers.html`, `materials.html`, `delivery_persons.html`, `delivery_rents.html`, `stock_summary.html`, `basket_view.html`, `pending_bills.html`, `unpaid_transactions.html`, `notifications*.html`, `void_audit.html`, `bill_collision_resolution.html` | Mostly utility soup (`bg-dark text-white border-secondary`) + `theme.css` re-skin | Hover rows, one table contract, filter bars, badges, empty states |
| **E. Ledgers** | `client_ledger.html`, `supplier_ledger.html`, `supplier_ledger_shift.html`, `financial_ledger.html`, `material_ledger.html`, `ledger.html`, `decision_ledger.html` | Inline kits (400–900 l) | Toolbar rhythm, sticky/scroll behaviour, summary cards, balance emphasis |
| **F. Finance & recon** | `payments.html`, `pay_supplier.html`, `payment_receipt.html`, `supplier_payment_receipt.html`, `cash_flow.html` (+`_differences`, `_reconciliation_detail`), `daily_reconciliation.html` (+`_daily_recon_board`), `financial_tracker.html` (+`_financial_tracker_matrix`), `financial_details.html`, `profit_entries.html`, `profit_reports.html`, `view_bill.html` | Mixed: `.ui-v2` (cash_flow), private kits (payments, view_bill) | Shared KPIs, matrix/recon tables, bill sheet (view_bill), receipt pages |
| **G. Accounts module** | `accounts/*` (24 templates incl. KPI partials, forms, ledger, audit) | `_acc_styles.html` fourth kit | Unify onto shared tokens (keep `.acc-*`/`.kpi-card` class names) |
| **H. Import / migration tools** | `import_export.html`, `import_export_new.html`, `import_jumble.html`, `import_review.html`, `full_raw_import_history.html`, `legacy_migration*.html`, `old_data_migration*.html`, `data_lab.html`, `system_report.html`, `app_upgrade.html` | `ui-v2` (import_export_new) vs utility soup | Stepper/wizard chrome, progress rows, diff/review tables, consistent buttons |
| **I. Admin / root / settings** | `settings.html`, `tenants.html`, `login_sessions.html`, `activity_log.html`, `admin_modules.html`, `root_backup_settings.html`, `root_recovery*.html`, `tenant_backup_history.html` | Utility soup + role modal kit (`ui.css:1263+`) | Form-check rhythm (settings has 41 checks), card sections, code/recovery blocks |
| **J. Print & PDF** | `client_ledger_print.html`, `financial_ledger_print.html`, `supplier_ledger_print.html`, `pdf_report.html`, `client_full_history_pdf.html` | Print-only inline | Black-on-white contract, `@page` margins, no theme leakage into print |

Every family inherits Phase 1–2 (shared fixes). Phases 3–11 then sweep family by family.

---

## 5. Target rules (the contract to implement)

### 5.1 One token home
All tokens declared in `theme.css` `:root` / `[data-theme="light"]` **only**. `ui.css` maps its `--ui-*` names to them (or is edited to read the shared names directly). Forbidden after Phase 1: any second declaration of `--ui-field-h*`, colour hexes in page styles, radius literals outside the scale.

### 5.2 Control scale (matches Direct Sales plan §4.1)
| Token | Desktop | ≤575 px | Used by |
|---|---|---|---|
| `--ui-field-h` | **40 px** | **34 px** | inputs, selects, date shells, default buttons |
| `--ui-field-h-sm` | **34 px** | **30 px** | filter bars, compact row controls, `btn-sm` |
| `--ui-field-h-xs` (new) | **30 px** | **30 px** | table action icon buttons |

### 5.3 Type scale (8 steps)
`0.72` (hint) / `0.78` (label, uppercase, tracking .04em) / `0.875` (body-sm, buttons) / `1rem` (body) / `1.15` (card title) / `1.35` (page title) / `clamp(1.35–1.75)` (section hero) / `clamp(1.6–2.1)` (KPI numeral). Micro-labels below 0.72rem are banned; existing 0.62–0.71 rem values snap to 0.72.

### 5.4 Radius & space
Radii: **6 px** (chips/inputs-sm) / **10 px** (controls, `--ui-radius`) / **16 px** (cards, `--ui-radius-lg`) / **pill** (badges, chips). Gaps: `--ui-gap-xs/sm/md` (0.35/0.5/0.75) + page gutter `--ui-page-gutter: 24px` (16 px ≤768). Card padding 16 px (list/filter cards) / 22 px (dashboards).

### 5.5 One input palette + theme-aware chevron
One surface pair (`--input-bg` / `--input-border`) consumed by every control; `background-color` (never `background`) + `background-image: var(--ui-select-chevron)` with per-theme SVG data-URI (light stroke on dark theme, dark stroke on light), `background-size: 14px 10px`, `background-position: right .75rem center`. Applies to `.form-select` everywhere including `input-group` and readonly-wrapped selects.

### 5.6 Table contract
One `.table` restyle in `theme.css`/`ui.css`: header 0.72rem uppercase on `--surface-2`; row min-height 44 px; `tbody tr:hover > td` with **accent tint + `!important` that beats the row-bg rule** (or the row-bg rule rewritten to `td` so hover can live on `tr`); zebra optional via `color-mix`; numeric cells `text-end` + tabular-nums; badge chips 0.68rem; actions column nowrap; pagination active = accent fill; mobile ≤767: shared card-stack (extend `ui.css:880-940` beyond `.ds-sales-table` to `.table-stack-cards` and adopt it page by page). Sticky headers only where a scroll container exists (`max-height` on the wrapper) — otherwise delete `position:sticky`.

### 5.7 Filter-bar contract
12-column `form-grid-top`; labels top-aligned (keep `theme.css:264-270`); **actions row horizontal** (`flex-direction:row; justify-content:flex-end; gap:8px`) via a scoped `.filter-actions` override; Apply = primary accent, Reset = secondary; Rows select min-width 92 px.

### 5.8 Focus-visible standard
`box-shadow: 0 0 0 3px color-mix(in srgb, var(--ui-accent) 35%, transparent)` + accent border on `:focus-visible` for inputs/selects/buttons/links/cards; delete `box-shadow:none`/`outline:none` kills; keyboard-only (keep `:focus-visible`, not `:focus`).

### 5.9 KPI & page-header contract
`.ui-kpi` and `.kpi-card` converge on: radius 16, numeral clamp 1.6–2.1rem, label 0.72rem uppercase, min-height 96–110, one watermark icon, hover lift `--ui-shadow-hover`. Gradient fills allowed **only** as accent-tinted `color-mix` surfaces derived from tokens (replace the hard-coded emerald/rose/… hex pairs in `_acc_styles.html` with token mixes). `.ui-page-header` and `.acc-page-header` share title/subtitle/toolbar rhythm (the gradient header becomes a token tint).

### 5.10 Light-mode rules
One `[data-theme="light"]` block per component, classes that exist, no attribute-selector additions. Combobox, flatpickr, modal close, badges, and print styles each get one such block in Phase 2/12.

### 5.11 `!important` budget
`!important` is legal **only** when beating a Bootstrap utility or an earlier global rule (documented with a one-line comment). Target: −50 % overall by Phase 12; zero in page `<style>` blocks except documented overrides of `theme.css` globals.

---

## 6. Phased execution plan (CSS-only; one commit per phase)

### Phase 0 — Baseline (no code)
Screenshots of every family at 1440/1366/1024/768/390 in **both** themes from the running app (100 % and 125 % zoom). Store outside git (or a wiki page); they are the before/after proof.
**Acceptance:** matrix in §8 exists, pages load, no behaviour changes.

### Phase 1 — Token unification (zero visual change)
Move `--ui-field-h*`/gap/label tokens to one home (`theme.css`), keep current computed values (40/34 desktop, 34/30 mobile); `ui.css` stops re-declaring; add `--ui-field-h-xs:30px`, `--ui-select-chevron` (both themes), radius/type aliases. **No selector changes.**
**Acceptance:** identical screenshots to Phase 0 (diff = 0 pixels), every page; `grep` shows one declaration per token.

### Phase 2 — Global component contract (the 60 % win)
One commit-set, each a small reversible commit:
1. Select chevron: replace `background` shorthand with `background-color` + theme-aware `background-image` at `ui.css:296/486-490/1278` and the sale-sheet rules (sale sheets per their plan, same patch).
2. Table hover: re-target `theme.css:98-100` (paint `td`, hover `tr:hover > td`) so every `.table` gets hover in both themes.
3. Pagination active state after `theme.css:371-375`.
4. Input palette: map `--input-bg` ≡ shared input token; kill the third grey; `.badge.bg-dark`, `.border-secondary` keep their token mapping (`theme.css:145-149`).
5. Combobox: token-driven `--combobox-bg/hover/border` replacing `#ffe7c2/#dcfce7` (`theme.css:385-398`); scope-wide, both themes.
6. Focus-visible standard (§5.8) in `theme.css`, remove known `box-shadow:none` kills in inline blocks.
7. Filter actions horizontal (§5.7) — one rule change + scoped override.
8. Type/radius snap: token remap only (9→10 px radii, micro-fonts→0.72rem) via the alias tokens.
**Acceptance:** chevron visible on every select app-wide; hover on every table; active page highlighted; Apply/Reset side by side on filter pages; both themes checked; `pytest` green.

### Phase 3 — Shell (family A)
`layout.html` inline → tokens: sidebar 260 px (240 ≤1200, off-canvas ≤991), topbar 56 px, `.nav-link` height/radius/active accent bar, submenu indent rhythm, mobile overlay; `login.html` card radius 16, token shadow (drop hard `rgba(0,0,0,.5)`), inputs via contract; `limited_access.html` matched.
**Acceptance:** nav consistent at all widths; login card identical proportions in both themes.

### Phase 4 — Dashboards (family B)
KPI/header unification (§5.9) applied to `index.html`, `admin_dashboard.html`, `accounts/*` KPI partials + `_acc_styles.html`; card gaps 16/22; hero numerals clamp; CTA hierarchy (one primary per card row).
**Acceptance:** side-by-side dashboards look like one product; gradients become token tints; 390 px tiles stack 1/row.

### Phase 5 — Transaction screens (family C)
`bookings.html` item-grid heights → tokens (38/36 → 40/34/30); `material_returns.html`, `grn_wizard.html`, `dispatching.html`, `receiving.html`, `daily_transactions.html`, `mixed_transactions.html`: equal field heights per item row, shared sheet chrome (modal header/body/footer rhythm), chevrons, focus rings; Direct Sales untouched here (its plan).
**Acceptance:** every item-row box equal height per screen; no clipped field at 768/991/1024 (wrap, don't clip — same recipe as Direct Sales Phase 3).

### Phase 6 — List / master pages (family D)
Adopt the table contract (§5.6) + filter contract (§5.7): `clients.html`, `suppliers.html`, `materials.html`, `delivery_*`, `stock_summary.html`, `pending_bills.html`, `unpaid_transactions.html`, `notifications*`, `void_audit.html`, `bill_collision_resolution.html`. Mobile card-stack for the widest 5 tables first. Badge/empty-state styles from one place.
**Acceptance:** hover + pagination + filters per §8 shared rows; 1366 without horizontal scroll where content allows; card-stack at 390 on the big 5.

### Phase 7 — Ledgers (family E)
`client_ledger`, `supplier_ledger`, `supplier_ledger_shift`, `financial_ledger`, `material_ledger`, `ledger`, `decision_ledger`: shared toolbar rhythm (per `ui.css:824-836` ledger-toolbar note), summary-card metrics, balance/emphasis typography (tabular-nums), sticky header rule where a real scroll container exists.
**Acceptance:** all seven ledgers share identical header/toolbar/summary geometry; balances right-aligned and legible at 125 % zoom.

### Phase 8 — Finance & recon (family F)
`payments.html` (modal padding/field heights → tokens; `.entry-note` truncate 24ch → shared notes style), `pay_supplier.html`, receipts (`payment_receipt`, `supplier_payment_receipt`), `cash_flow*` (already `.ui-v2` — verify-only + token spot fixes), `daily_reconciliation*` board, `financial_tracker*` matrix, `financial_details`, `profit_*`, `view_bill.html` (bill sheet: one paper-like surface in-app, print styles to Phase 10).
**Acceptance:** one KPI/filter/table look across the family; matrix and recon tables readable at 1024 (pan_view acceptable below that).

### Phase 9 — Accounts module (family G)
`_acc_styles.html` rewritten to consume shared tokens (keep `.acc-*`/`.kpi-card` names): gradient header → token tint; `.kpi-card` colours → `color-mix` accent pairs; `.acc-section-card` radius 14→16; forms via `--ui-field-h*`; ledger/audit tables via table contract. 12 partial `<style>`s collapse into `_acc_styles.html`.
**Acceptance:** accounts pages sit visually beside `index.html`/`cash_flow` as the same app; both themes; zero hard hexes left in `_acc_styles.html`.

### Phase 10 — Tools (family H) + print (family J)
Import/export & migration pages: shared stepper/wizard chrome, progress rows, review/diff tables, drop per-page button styles; `app_upgrade`/`system_report` matched. Print pages: one `@media print` block in `theme.css` (white bg, black text, hidden chrome, `@page` margin 12 mm), remove per-page print CSS duplicates.
**Acceptance:** all wizards share one step/progress look; `client_ledger_print`/`pdf_report` etc. render identical print output before/after (print CSS is replace-equal, not restyle).

### Phase 11 — Admin / root / settings (family I)
`settings.html` form-check grid rhythm (41 checks), `tenants.html`, `login_sessions.html`, `activity_log.html`, `admin_modules.html`, `root_*`, `tenant_backup_history.html`: section cards, code-block/recovery styles tokenised, role modal kit already in `ui.css:1263+` adopted for consistency.
**Acceptance:** dense settings pages scannable at 1024; checks in aligned columns; recovery codes monospace block consistent.

### Phase 12 — Sweep & guardrails
Delete dead rules (e.g. `theme.css:486-540` legacy sale-form light block — superseded by the Direct Sales plan), duplicate `thead th` blocks, unused font-sizes; `!important` audit (comment or remove) to the §5.11 budget; remove attribute-selector hacks **only** where the targeted inline hexes were replaced by tokens in the same commit; final light-mode pass; 125 %-zoom pass; write the "CSS rules" comment block at the top of `theme.css` (token names, scale, breakpoints, `!important` policy).
**Acceptance:** §8 matrix green in both themes at 100 %/125 %; `grep` counts: hex-in-templates ≈ 0 in touched files, one token declaration each; `pytest` green.

**Order:** 0 → 1 → 2 first (global contract), then families A→B→C→…→I (each independently shippable), 12 last. Direct Sales implementation runs **in parallel under its own plan**, consuming Phase 1–2 outputs.

---

## 7. Verification matrix

Shared rows (every family, every phase), widths **1440 / 1366 / 1024 / 768 / 390**, themes **dark / light**, zoom **100 % / 125 %**:

| Check | 1440 | 1366 | 1024 | 768 | 390 |
|---|---|---|---|---|---|
| Select chevron visible | ✔ | ✔ | ✔ | ✔ | ✔ |
| Table row hover visible | ✔ | ✔ | ✔ | ✔ | cards |
| Pagination active state visible | ✔ | ✔ | ✔ | ✔ | ✔ |
| Field heights 40/34/30 | ✔ | ✔ | ✔ | ✔ | ✔ |
| Filter actions side by side, under inputs | ✔ | ✔ | ✔ | ✔ | stacked (full-width) |
| Focus ring visible (keyboard tab) | ✔ | ✔ | ✔ | ✔ | ✔ |
| Combobox readable (both themes) | ✔ | ✔ | ✔ | ✔ | ✔ |
| No horizontal page scroll | ✔ | ✔ | ✔ | ✔ | ✔ |
| Light mode: no dark leftovers / no cream popups | ✔ | ✔ | ✔ | ✔ | ✔ |

Family-specific rows: dashboards — KPI tiles equal metrics; transactions — item rows equal height, fields wrapped not clipped at 768/991; ledgers — toolbar on one line (wrap ok), balances aligned; tools — wizard steps aligned; print — unchanged output; settings — check grids aligned.

Run `pytest` after every phase (no CSS tests exist; regression safety is the suite + the screenshot diff + this matrix). Phase 0/12 screenshots must be diffed pairwise.

---

## 8. Risks & guardrails

| Risk | Mitigation |
|---|---|
| `theme.css` is global — a token edit moves every page | Phase 1 keeps **currently computed** values (40/34, 34/30) → zero visual change; only then do families opt into new rules phase by phase |
| Global `.btn`/`.form-control` rules use `!important` | Never edit their sizes per page; scope overrides under page containers (`.booking-modal-dialog`, `.sale-form`, `.acc-*`) exactly as existing pages already do |
| 57 inline `<style>` blocks duplicate rules | Each family phase deletes its page-local duplicates **after** the shared rule exists (same commit), never before |
| Attribute-selector hacks react to inline hexes | Do not touch inline hexes and the hacks in different commits; Phase 12 pairs them |
| Light mode regressions hide in `!important` chains | Every phase ends with the light-mode row of §7; one `[data-theme=light]` block per component only |
| CSS-only cannot fix structural table overflow (e.g. 11-col sales table) | Record markup fix as backlog (Direct Sales plan Option B); CSS approximation: density + hide-lowest-value columns below 1600 + pan_view |
| pan_view.js hijacks horizontal scroll | Leave `.ams-pan-*` behaviour untouched except its fade/fit chrome; verify wide tables still fit-to-width after table-contract changes |
| Print styles break with token colours | Phase 10 print block forces white/black explicitly; verify receipts/ledgers print identical |

---

## 9. Priorities (what to ship first if time is short)

1. **Phase 1 + 2** — tokens, chevron, hover, pagination, combobox, focus, filter actions. Touches all 120 templates for ~8 small commits.
2. **Phase 3 + 4** — shell and dashboards (first screens every user sees).
3. **Phase 6 + 7** — the daily work surfaces (lists + ledgers).
4. **Phase 5 + 8 + 9** — forms, finance, accounts.
5. **Phase 10 + 11 + 12** — tools, admin, sweep.

---

## 10. Out of scope (explicitly not CSS)

- Markup restructures (sales-table Option B, kebab action menus, `for`/`id` label pairs — backlog items owned by their page plans).
- JS/behaviour (lazy modals, CSRF injector, combobox logic, pan_view behaviour).
- Backend, routes, permissions, data.
- New features (voided-tab, per_page=100 clamp, etc. — noted in the Direct Sales plan §3.3).

---

## 11. Confirmed decisions

| Decision | Choice |
|---|---|
| Scope | **CSS-only** (stylesheets + `<style>` blocks); markup/JS/Python untouched |
| Token home | **`theme.css` only**; `ui.css`/page styles consume |
| Control scale | **40 / 34 / 30 px** (desktop / compact / mobile) |
| Direct Sales | Governed by `DIRECT_SALES_CSS_PLAN.md`; shared fixes land in Master Phase 1–2 |
| KPI look | Keep both class names (`.ui-kpi`, `.kpi-card`) — one visual contract behind them |
| Themes | Dark and light both first-class; verified every phase |
| Execution | Phase 0 → 1 → 2, then families A–I, sweep last; one commit per phase-step; `pytest` after each |

**Start order:** Phase 0 screenshots → Phase 1 tokens (zero-diff) → Phase 2 global contract (the visible win). Everything else is family polish on a stable base.
