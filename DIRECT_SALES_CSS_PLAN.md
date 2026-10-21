# Direct Sales — CSS / Field-Size Review & Fix Plan

**Status:** Plan finalized — decisions confirmed. Shared items (token de-duplication, select-chevron background, pagination states) have since landed via `MASTER_CSS_PLAN.md` Phase 1–2; the page-specific work below is still pending.
**Page:** `/direct_sales` (list + Add Sale sheet + Edit Sale sheet)

**Confirmed decisions**
- Sales table → **Option B**: 8 compact columns + kebab action menu.
- Control scale → **40 px** desktop / **34 px** compact / **30 px** mobile.
- Scope → shared fixes allowed (token de-duplication, select background, pagination), each as its own commit.
- Form container → keep the **full-screen** sale sheet.

**Files in scope**

| File | What it contributes |
|---|---|
| `templates/direct_sales.html` | 3 inline `<style>`/markup blocks, 3,059 lines |
| `templates/_direct_sale_edit_modal.html` | Duplicate of the sale sheet (404 lines) |
| `static/ui.css` | Design tokens + `.ds-*` page chrome (1,374 lines) |
| `static/theme.css` | Legacy theme mapping, global form/button sizing (593 lines) |
| `templates/layout.html` | Combobox CSS/JS, layout gutters, page wrapper |

**Method:** static reading of the full cascade (Bootstrap 5.3 → theme.css → ui.css → inline page CSS), with computed heights/widths derived from the actual declarations. No browser was available in this environment, so the first implementation step is a screenshot pass at the widths listed in §6 to confirm the measurements.

---

## 1. Executive summary

The sales screen is styled by **four different layers written at different times**, each trying to fix the previous one. The result is not one design system; it is a pile of overrides. The most important problems:

| # | Severity | Problem | Effect |
|---|---|---|---|
| 1 | **P0** | The line-item grid has a hard 750 px floor, but ≈992–1100 px screens give it ≈700–755 px; the 768–991 px rule has no floor at all. `overflow-x: hidden` hides the result. | At ≈992–1100 px fields are **clipped and unreachable**; at 768–991 px they **collapse to 40–60 px slivers**. No scrollbar appears in either case. |
| 2 | **P0** | Field heights in one line item are **34 / 36 / 40 / 42 px** at the same time (buttons, `input-group-sm`, default inputs, padded qty input). | The row looks broken; the most-used form on the page is visually noisy. |
| 3 | **P1** | The filter bar has **13 grid columns** at `lg` (12 available) and the action cell is forced to `flex-direction: column`. | Apply/Reset fall onto a second line and stack vertically instead of sitting under the inputs. |
| 4 | **P1** | `.sale-form .form-select { background: … !important }` uses the **`background` shorthand**, wiping Bootstrap's `background-image` chevron. | Selects have **no dropdown arrow** in the sale sheet (and `.ui-form` / `.ui-v2` pages have the same bug). |
| 5 | **P1** | `theme.css` sets `.table tbody tr { background: … !important }`. | All row hover states and the mobile card tint written in `ui.css` are **dead**. |
| 6 | **P1** | The sales table has **11 columns and up to 7 action buttons per row**, with no column sizing (~1,100–1,200 px min-content vs ~1,030–1,104 px available). | Actions wrap into 2–3 lines (tall rows) or the right side scrolls out of view on 1366/1440 laptops. |
| 7 | **P1** | `theme.css` pagination rule uses `!important`. | The **active page has no highlight** — `bg-warning` is overridden. |
| 8 | **P2** | Five label styles, 32 font sizes, 6 corner radii, `.ui-card` padding 14/18/22 introduced ad hoc. | The page has no vertical rhythm; labels do not line up between the filter bar and the sheet. |
| 9 | **P2** | CSS is duplicated: `.ds-sales-table thead th` twice, sale-sheet chrome in 3 places, light-mode rules for classes that no longer exist. | Every fix must be made 2–4 times, and regressions are invisible until someone opens the other theme. |
| 10 | **P2** | 53 redundant `bg-dark text-white border-secondary` class strings in the two sale forms; `!important` counts: **ui.css 72, theme.css 133, inline page 31** (236). | Theme bugs cannot be diagnosed; light mode only works because of specificity accidents. |

**Verdict:** the page should not be "polished" selector by selector. It needs **one field-size contract**, **one responsive strategy for the item row**, and **one cleanup pass** that deletes duplicated/dead rules. The plan in §5 does exactly that in 6 small, reversible phases.

---

## 2. How the CSS is layered (why conflicts happen)

```
bootstrap.min.css
  └─ theme.css        global colours, --ui-field-h:38px, .btn heights, tables, light-mode !important
       └─ ui.css      :root tokens (re-declares --ui-field-h:40px), .ds-* page chrome, table
            └─ direct_sales.html  <style> #1   sale sheet, item grid
                 └─ direct_sales.html  <style> #2   compact item row  ← wins source-order ties
```

A few `theme.css` light-mode rules still beat all of the above because they use `!important` — even though that file loads first.

Two consequences:

1. **Tokens are declared twice.** `theme.css:24-25` sets `--ui-field-h: 38px; --ui-field-h-sm: 32px`, then `ui.css:45-46` sets `40px / 34px`. Because `ui.css` loads later, the real values are **40/34**, and `theme.css`'s own controls quietly use 40. On mobile, `theme.css:299-300` sets **34/30**, but `ui.css` has no mobile override — so `.ui-form` pages (if any used it) would stay at 40 on phones.
2. **`!important` is doing the theming.** `theme.css` has 133 of them; the page adds 31 more. Many rules are written to out-`!important` the previous layer, which is why hover/pagination/chevron/`text-info` are silently lost (§3.4).

---

## 3. Detailed findings

### 3.1 Layout & responsive (P0)

**F1 — The line-item grid breaks at every width below 1200 px.**
- `templates/direct_sales.html:42-46` — 7 tracks with a hard floor of ≈ **750 px** (`120+100+120+120+100+90+40 + 6×10 gaps`).
- `@media (max-width:1199.98)` (`:182-186`) repeats the same `minmax()` floor.
- `@media (max-width:991.98)` (`:188-193`) **drops `minmax()`**: the tracks become `1.2fr 1fr 1.2fr 120px 1.2fr 1fr auto`. Because `.sale-item-grid > *` sets `min-width: 0` (`:69-73`), the fields no longer overflow — they **collapse to ~40–60 px slivers**, hiding placeholders and values.
- `templates/direct_sales.html:10` — `.sale-form .modal-body { overflow-x: hidden; }` silently clips whatever does overflow.
- Available width for the grid: after the 1.5 rem body padding, the 1 rem row gutter, `ps-md-3` (1 rem) and the compact row's 0.6 rem side padding, a **≈992–1100 px viewport leaves ≈700–755 px** — at or below the 750 px floor, so "Item Total" and Remove are clipped with no scrollbar for the user to find them. **768–991 px ⇒ no floor**, so Material / GRN / Alternate / Rate collapse to 40–60 px. **≥1200 px fits.**
- **Result:** every tablet and small-laptop width is unusable — either clipped or compressed. This is a functional defect, not a cosmetic one.

**F2 — Filter row is 13 columns and the actions cell is a vertical stack.**
- `direct_sales.html:275-336`: `2+2+2+2+2+1+2 = 13` columns at `lg` (`<div class="col-lg-2 … d-flex gap-2 align-items-end">`).
- `theme.css:273-277` forces every `.ui-helper-form .row.form-grid-top > [class*="col-"]` to `display:flex; flex-direction: column; justify-content: flex-start`.
- `.d-flex` only sets `display`; the direction stays `column`, and `align-items-end` becomes horizontal alignment. So Apply and Reset are **stacked vertically, right-aligned, level with the labels**, and the cell itself wraps to row 2 at `lg`.
- At `sm`, the same rule makes the 12-wide action cell a stack too; the existing mobile rule (`.ui-helper-form .ui-actions-col .btn { width:100% }`) does not match because the markup does not use `.ui-actions-col`.

**F3 — Three independent scroll areas in one modal.**
- `#sale-items-container { max-height: calc(100vh - 540px); min-height: 180px; overflow-y: auto }` (`direct_sales.html:92-99`). The 540 px is a magic number that does not track the real header/footer/summary heights. At 768 px viewport height the container is 228 px; below 720 px the `min-height: 180px` wins and the form overflows.
- Inside that, `.delivery-person-rows { max-height: 200px; overflow-y:auto }` (lines 131-137) and `.bookingStatusContainer { min-height:220px; overflow-y:auto }` (line 591).
- Result: nested scrollbars; "Add More Items" scrolls away while the item list scrolls separately.

**F4 — Left panel keeps a right border on phones.**
`direct_sales.html:543` uses `border-end` (all breakpoints) + `pe-md-3` (md+ only). Below 768 px the columns stack, leaving a stray vertical line at the right edge of the sale-type/client block.

**F5 — Sticky table header never sticks.**
`ui.css:848-853` sets `position: sticky; top: 0` on `thead th`, but its nearest scroll container is `.table-responsive` (`overflow-x:auto` ⇒ `overflow-y:auto`), which has **no height limit and never scrolls**; `.ui-card` also has `overflow:hidden`. So the header scrolls away with the page. Either give the wrapper a `max-height` + `overflow:auto`, or remove the sticky rule.

**F6 — Mobile "Unit Price" caption overlaps the input.**
- `direct_sales.html:982-987`: `.rate-cell { display:flex; align-items:center; height:34px }`.
- `direct_sales.html:58-74` defines a `::before` caption for every grid cell; it is switched on at ≤767 px (`:195-201`).
- A 1 rem caption + a 36 px input inside a fixed 34 px box ⇒ the caption is drawn outside the cell and collides with the previous row. `.rate-cell.reserved-only` already resets `height:auto`, which confirms the fixed height is the bug.

### 3.2 Field and box sizing (P0–P1)

**F7 — No single control height inside one line item.** Actual computed heights:

| Element | Declared by | Height |
|---|---|---|
| Material / GRN / Alternate input | `.input-group-sm > .form-control` (`direct_sales.html:227-232`) | **36 px** |
| Qty input | inline `style="padding:0.475rem…; font-size:1.05rem"` (`:730`) | **≈42 px** |
| Qty −/+ buttons | `.btn-sm` (`theme.css:195-199`) | **34 px** |
| Unit Price input | `.form-control-sm` + the 36 px rule | **36 px**, but its cell is `height:34px` |
| Item Total input | `.form-control-sm` | **36 px** |
| Remove button | `.btn-sm` | **34 px** |
| Delivery select/inputs | default `.form-control` | **40 px** |
| Delivery "X" button | `.btn-sm w-100` | **34 px** |
| Filter inputs | `.form-control-sm` | **34 px** |

So a single page uses 34, 36, 40 and 42 px controls. The user perception "boxes look different sizes" is literally true.

**F8 — The 36 px rule is a band-aid.**
`direct_sales.html:227-232` hard-codes `min-height:36px` for small controls — a hand-tuned compromise between the global small height (34) and the default (40). It sits outside the token system, so any token change breaks it again.

**F9 — Sale sheet and filter bar use different palettes.**
- Filter inputs: `theme.css:150-157` → `background: var(--input-bg)` (`#0f172a` dark / `#ffffff` light), border `--input-border`.
- Sale sheet inputs: `direct_sales.html:234-239` → `background: var(--ui-surface-2)` (`#0f1730` / `#eef2f9`), border `--ui-border-strong`.
- The sale-date input uses a **third** surface `--ui-surface-3` (`:157-166`), and the same elements also carry `bg-dark`, which maps to `--surface-2` (`#020617` / `#e9eef5`).
Three greys for the same job; light mode is visibly inconsistent (white filter inputs vs blue-grey sheet inputs).

**F10 — The date shell mixes three surfaces and has no focus state.**
`.sale-date-shell` is radius 10 px + `overflow:hidden` with a `--ui-surface-2` shell (`:145-149`); the icon and the calendar button are transparent (`:150-152`), and the inner input is forced to a third surface `--ui-surface-3` with `border:0` and `box-shadow:none !important` (`:157-166`) — which also **kills the global focus ring**. The whole control reads as three different boxes, and keyboard users get no focus feedback. It is also pushed down by a hard-coded `.delivery-date-spacer { height:1.05rem }` (`:139-142`) that does not match the label/label-row line heights.

**F11 — Delivery row proportions are wrong.**
`delivery-cell { flex:1 1 0 }` gives the delivery-person `<select>` the same width as "Bags" and "Rent"; the remove column is a fixed 56 px. Long names truncate while two numeric fields get ~1/3 of the row each. The labels row and the scrollable rows list are separate flex containers with different widths (labels 100%, rows 100% minus scrollbar + 4 px), so "Remove" drifts from its button once the list scrolls.

**F12 — The modal footer ignores its own utilities.**
`theme.css:180-186` sets `.btn { min-height:var(--ui-field-h) !important; padding:.42rem .9rem !important }` (effective 40 px desktop). The footer buttons use `py-2`, `rounded-pill`; `py-2` is dead (overridden by the `!important` padding), so the footer height is set by the theme, not by the markup. `rounded-pill` survives only because Bootstrap also uses `!important`.

**F13 — The "Item Total" is not coloured.**
`text-info` on the read-only total (`direct_sales.html:744`) is overridden by `.sale-form .form-control { color: var(--ui-text) !important }` (`:234-239`, higher specificity + important). The intended cyan total never appears.

### 3.3 Table (P1)

**F14 — 11 columns, no column contract, action buttons wrap.** The table has no `table-layout`, no per-column widths and no `min-width`; layout is decided by content on every render:

| Col | Content | Preferred width |
|---|---|---|
| Bill No. | badge (CASH-SALE) | 104 |
| System Ref | badge | 114 |
| Status | badge | 104 |
| Client | name + optional badge | 164 |
| Date & Time | date + time stacked | 119 |
| Material | item names + Booked/Due badges | 130 |
| Qty | qty + "N booked of M dispatched" | 120 |
| Total Amount | `currency + amount` | 144 |
| Driver Rent | amount | 144 |
| Notes | 20ch truncated (`ui.css:868-878`) | 184 |
| Actions | 3–7 buttons, 34 px + 2 px border | 204–292 |

Sum of preferences ≈ **1,530 px** (≈ max-content); min-content is ≈ **1,100–1,200 px** depending on bill numbers and names. Content width is ≈1,104 px at a 1440 viewport and ≈1,030 px at 1366 (`layout.html` sidebar 260 + 30 px padding each side). So on a normal yard laptop the table is **at or past its limit**: the Actions column is cut off (horizontal scroll) or — more often — the 5–7 buttons **wrap into 2–3 lines**, making every row 100+ px tall. Either way the table is hard to scan.

**F15 — Dead row hover / dead card tint.** `theme.css:98-100` `.table tbody tr { background: var(--table-row) !important }` beats `ui.css:684-686` `tr:hover` (no `!important`) and `ui.css:889` mobile `tr { background: var(--ui-surface-2) }`. Mobile cards become plain `--table-row`, not the tinted surface they were designed to be.

**F16 — Centred ID columns + 34 px pill buttons + `me-1` margins.**
Three centred badge columns (`Bill No.`, `System Ref`, `Status`) create a ragged centre line; the action cell holds up to **7** `rounded-pill` buttons with 4 px margins plus 2 px borders, which reads as a control strip rather than row actions. The Delete control is the only text button, so it breaks the icon rhythm and is the widest element.

**F17 — Notes are over-truncated.** `max-width: 20ch` (`ui.css:868-878`) forces a 184 px column for a field that is usually short; the full text is only available via the native `title` tooltip (not touch-friendly).

**F18 — Pagination active state is invisible.** `theme.css:371-375` sets `background-color: var(--surface-2) !important; color: var(--text-primary) !important` on `.pagination .page-link`; the active item uses `bg-warning border-warning text-dark` (utilities, lower specificity). The current page therefore looks like every other page link.

**F19 — The "Rows: 100" option never works.** The view clamps `per_page` to 50 (`_direct_sales_direct_sales_page.py:15`) but the select offers 100 (`direct_sales.html:329-334`). Not CSS, but it is part of the same box and should be fixed in the same pass (remove the option or raise the cap).

### 3.4 Cascade / theme defects (P1)

**F20 — Select chevrons are wiped.** `.sale-form .form-select { background: var(--ui-surface-2) !important }` (`direct_sales.html:234-237`) resets `background-image` (Bootstrap's chevron) to `none`. The same shorthand is used in `ui.css:296` (`.ui-form .form-select`) and `ui.css:485-490` (`.ui-v2 .form-select`, used by 7 templates). Note that Bootstrap's default chevron is `#343a40`, so even when it survives it is invisible on dark inputs — a per-theme chevron is needed, not just `background-color`.

**F21 — Light-mode rules for classes that no longer exist.** `theme.css:487-540` targets `.summary-material`, `.field-label`, `.mini-cell-label`, `.item-total-label`, `.item-total-value`, `.item-total-box`, `.line-actions` — none of these appear in `direct_sales.html` or `_direct_sale_edit_modal.html`. Dead weight, and it hides the fact that the live light-mode support is only `label { color: … !important }`.

**F22 — The same rule is written 2–4 times.**
- `.ds-sales-table thead th`: `ui.css:667-676` and `ui.css:849-853`.
- Sale-sheet chrome: `ui.css:557-571` (`.modal-content.ds-sale-sheet`), `direct_sales.html:17-23` and `theme.css:505-511`.
- Item grid: `direct_sales.html:42-74` + 3 media queries + `direct_sales.html:982-1034`.
- 4 light-mode `!important` blocks for the same screen.

**F23 — Attribute-selector hacks.** `theme.css:400-460` rewrites inline colours by matching `[style*="background: #0f172a"]` etc. This is fragile (spacing variants already need duplicated selectors), hurts matching performance, and silently re-colours any unrelated element with the same inline string.

**F24 — 53 utility classes that do nothing.** `bg-dark text-white border-secondary` appears 25 × in the Add sheet and 28 × in the Edit sheet. `.sale-form .form-control` (0,2,0 + `!important`) already beats all three utilities, so removing them is visually **zero-risk** and removes 53 sources of confusion. The same applies to `.compact-item-row`'s `bg-dark`.

**F25 — Hard-coded accent dependent on the dark theme.** Focus ring `rgba(251,191,36,.15)` (`direct_sales.html:242`) stays amber in light mode, where the accent is `#b45309`; it is nearly invisible. The stale fallback `var(--ui-accent, #22d3ee)` on the calendar icon (`:156`) also shows this file was copied from a cyan-themed page.

**F26 — The peach combobox in a dark sheet.** `theme.css:387-398` forces `.combobox-list` to `#ffe7c2`; `ui.css` has a theme-aware version but it is scoped to `.ui-v2` / `.ui-combobox-v2` (`ui.css:458-480`), neither of which exists on this page. So opening Material/Client shows a bright cream popup over a dark modal (and greys in light mode).

### 3.5 Consistency, hierarchy, accessibility (P2)

- **F27 — Label styles:** `.ui-label` (0.78 rem, uppercase, 4 px margin), `theme.css` `.text-white-50.small.fw-bold` (min-height 1 rem, 0.35 rem margin), `.delivery-row-labels` (0.7 rem uppercase), `.sale-item-header` (0.7 rem uppercase + letter-spacing), `::before` captions (0.62 rem). Five systems for "a label".
- **F28 — Type scale:** 32 distinct `font-size` values are declared across `direct_sales.html` + `ui.css`, including 0.62/0.65/0.66/0.67/0.68/0.69/0.71/0.72/0.73/0.74/0.75/0.76/0.78 rem for labels and helper text. On phones, 0.62–0.65 rem ≈ 10 px — below a comfortable reading size.
- **F29 — Radii:** 8 px (`btn-sm`), 9 px (`btn`), 10 px (`--ui-radius`), 12 px (mobile cards), 16 px (`ui-card`), 999 px (pills) + `0.375rem` fallbacks. The visual language is not consistent between filters, tiles, table cards and the sheet.
- **F30 — Hierarchy:** the two clickable KPI tiles (`2.1rem` numbers, 22 px padding) plus the header CTA "Add Sale" plus the "Click to Add" pills give **three competing primary actions**. The table (the actual work surface) starts below ~430 px of chrome.
- **F31 — Accessibility:** ~20 modal `<label>`s have no `for`/`id` pair; `th` has no `scope`; icon-only action buttons rely on `title` (no `aria-label`); the modal is not referenced by `aria-labelledby`; focus rings are 15 % alpha; the sticky header consumes `z-index:5` for no benefit.

> Out of scope but noticed: the backend supports `show=voided` but the page only renders Active/All tabs; `per_page=100` is clamped (F19).

---

## 4. Target rules (the contract to implement)

### 4.1 One control-height scale

| Token | Desktop | ≤575 px | Used by |
|---|---|---|---|
| `--ui-field-h` | **40 px** | **34 px** | default inputs, selects, date shell, delivery inputs |
| `--ui-field-h-sm` | **34 px** | **30 px** | filter bar, compact line-item controls |
| `--ui-field-h-xs` | **30 px** | **30 px** | table action icon buttons (new) |

Rules:
- Declare these **once** (in `theme.css`), delete the duplicate from `ui.css:45-46`.
- A row may use only **one** of these heights. No `36px` anywhere; no inline padding that changes height; no fixed-height cell (`rate-cell`).
- `input-group` children inherit the same height (selector on the group, not on each child).
- Labels: `0.72rem`, uppercase, `700`, `letter-spacing:.04em`, `min-height:16px`, `margin-bottom:6px`.

### 4.2 Spacing & shape

| Purpose | Value |
|---|---|
| Space scale | 4 / 8 / 12 / 16 / 24 px |
| Card body padding | 16 px (filter bar 14 × 16 is fine if matched by tiles) |
| Tile padding / min-height | 16 px / 96 px, number `1.6rem` |
| Radii | 8 px (small controls), 10 px (inputs/cards), 14 px (large cards) |
| Table font | 0.86 rem; `th` 0.7 rem; cell padding 8 × 10 px |
| Item row control height | `--ui-field-h-sm` (34 px) uniformly |

### 4.3 Responsive strategy for the line item

| Width | Layout |
|---|---|
| ≥1200 px | 7-track grid, header row visible, captions hidden (as today, with a 750 px floor) |
| 768–1199 px | **flex-wrap** cells at `flex:1 1 190px`; header hidden; captions shown; remove button fixed 44 px |
| <768 px | same flex-wrap at `flex:1 1 100%`, captions shown, no fixed heights |
| Any width | `overflow-x` must never be the fallback; the sheet must never clip a field |

---

## 5. Fix plan (6 phases, smallest safe change first)

### Phase 0 — Baseline (30 min, no code)
1. Run the app, open `/direct_sales`, the Add sheet and one Edit sheet.
2. Capture at **1440 / 1366 / 1280 / 1199 / 1024 / 991 / 768 / 390** in dark **and** light.
3. In DevTools, record: item-grid scrollWidth vs clientWidth, every control height in one item row, table scrollWidth.
4. Keep screenshots outside Git (or in a local `screenshots/` folder that is git-ignored).

**Exit:** the numbers in §3 are confirmed or corrected before touching code.

### Phase 1 — Token consolidation (low risk)
- `static/theme.css`: set `--ui-field-h: 40px`, `--ui-field-h-sm: 34px`, mobile `34/30`. Remove `--ui-field-h*` from `ui.css:45-46` so there is one owner.
- Add a scoped block at the top of the sales CSS:
  ```css
  .sale-form { --sale-item-h: var(--ui-field-h-sm); }
  ```
- Replace the hard-coded `36px` (`direct_sales.html:227-232`) with `var(--sale-item-h)`.
- **Acceptance:** no declaration of `36px` for a control height remains; filter = 34, sheet = 40, item row = 34, mobile = 30/34.

### Phase 2 — Sale-sheet field & box normalisation
Files: `direct_sales.html` (first `<style>` + two markup snippets), `_direct_sale_edit_modal.html`.

1. **One size per line item**
   ```css
   .sale-form .sale-item-grid .form-control,
   .sale-form .sale-item-grid .form-select,
   .sale-form .sale-item-grid .btn,
   .sale-form .sale-item-grid .input-group-text {
       height: var(--sale-item-h);
       min-height: var(--sale-item-h);
       padding-top: 0; padding-bottom: 0;
   }
   ```
2. Remove the inline `style` on the qty input (`:730`); add `.qty-input { font-size:.95rem; font-weight:700; text-align:center; padding:0 .5rem; }`. Keep `btn-sm` for the −/+ buttons: at 34 px they now match the 34 px input.
3. `.rate-cell { height:auto; align-items:stretch; }`; keep the reserved-badge wrap.
4. **Delivery row → grid:** both the labels row and each data row become `grid-template-columns: minmax(0,1fr) 96px 110px 44px; gap:8px`. The sale-date section becomes `flex:0 0 320px` at ≥992; delete `.delivery-date-spacer`; align it with a `.delivery-row-labels` placeholder row.
5. **Date shell:** one height, `overflow:hidden` on the shell, input `background:transparent`; remove the third surface and **restore the focus ring** (delete `box-shadow:none !important`).
6. **Labels:** introduce `.ds-label` (same values as `.ui-label`) and apply it to the ~20 modal labels; add `for`/`id` pairs while touching them. Keep one helper style (`.ds-hint`, 0.72 rem).
7. **Selects:** `background-color` (not `background`), plus a theme-aware chevron:
   ```css
   .sale-form .form-select {
       background-color: var(--ui-surface-2) !important;
       background-image: var(--ds-select-chevron);
       background-repeat: no-repeat;
       background-position: right .75rem center;
       background-size: 14px 10px;
   }
   ```
   with `--ds-select-chevron` = light-stroke SVG in dark, dark-stroke SVG in light.
8. **Focus ring:** `box-shadow: 0 0 0 3px color-mix(in srgb, var(--ui-accent) 35%, transparent)`; drop the hard-coded amber.
9. **Remove height magic:** `#sale-items-container { max-height: min(420px, 42vh); min-height: 132px; }` only at ≥1200; `max-height:none` below. Same for the delivery list (`max-height: 260px`).
10. **Mobile side panel:** `border-end` → `border-md-end`; `col-lg-3 col-md-4` → `col-12 col-lg-3` (768–991 is too narrow for this rail).
11. **Cleanup in the same pass:** delete the 53 redundant `bg-dark text-white border-secondary` strings and the defeated `text-info` on the total (or force it with `.line-total.text-info { color: var(--ui-info) !important }` if the cyan is wanted).

**Acceptance:** within one line item every box is 34 px; the delivery inputs and the X button share a baseline; the date shell looks like one control; no clipped caption at 390 px; light mode uses one input palette.

### Phase 3 — Responsive item grid (fixes F1/F6)
```css
/* ≥1200: keep 7 tracks, enforce a 750px floor */
.sale-form .sale-item-grid { grid-template-columns: /* current minmax list */; }

/* <1200: wrap instead of clip */
@media (max-width: 1199.98px) {
    .sale-form .sale-item-header { display: none !important; }
    .sale-form .sale-item-grid { display: flex; flex-wrap: wrap; gap: 8px; }
    .sale-form .sale-item-grid > * { flex: 1 1 190px; min-width: 0; }
    .sale-form .sale-item-grid > *::before { display: block; }
    .sale-form .sale-item-grid > *:last-child { flex: 0 0 44px; align-self: flex-end; }
}
```
Also delete `overflow-x: hidden` from `.sale-form .modal-body`.

**Acceptance:** at 768/900/1024/1199 there is no clipped or hidden field; the row becomes 2–3 wrapped lines; at 1200+ the 7-column layout is unchanged.

### Phase 4 — Sales table (F14–F18)
**Chosen: Option B** (8 compact columns + kebab menu), because the table cannot be made comfortable by padding alone. Option A is kept below as a fallback if the markup change has to be reverted.

**Option A (fallback, CSS density only — still ~1,100–1,200 px):**
- `font-size:.86rem`, cell padding `8px 10px`, badge font `0.68rem`, action buttons 30 px, gap 4.
- Hide the Notes column below 1600 px (`@media (max-width:1599px) { th:nth-child(10), td:nth-child(10){display:none} }`) — data still reachable via edit.
- Fix hover on `td` with `!important` so it beats `theme.css`: `.ds-sales-table.table tbody tr:hover > td { background-color: … !important; }`.
- Make the header sticky *properly*: `.ds-sales-table-wrap { max-height: 65vh; overflow: auto }` on ≥992, or remove sticky.
- Pagination: add `.pagination .page-item.active .page-link { background-color: var(--ui-accent) !important; color:#1a120a !important; border-color: var(--ui-accent) !important; }` after `theme.css` and drop the `bg-dark/text-warning` utility soup.

**Option B (chosen, ~1,050 px):** small markup edits in `direct_sales.html`:
1. Bill No. + System Ref → one "Bill" cell (manual/auto stacked).
2. Material + Qty → one "Items" cell (`Material · Qty` lines).
3. Total + Driver Rent → one "Amounts" cell (total bold, rent muted).
4. Actions → Edit + a kebab dropdown (View, PDF, Print, Photo, URL, Delete).
5. Keep Status, Client, Date, Notes → **8 columns**.
6. Add `scope="col"` to every `th`; use `.ui-empty` for the empty row.

**Acceptance:** no horizontal scroll at 1366/1440; the primary row actions are visible without scrolling; hover works in both themes; the active page is clearly marked.

### Phase 5 — Filter bar & top-of-page hierarchy (F2, F30)
1. **Grid math:** Client 2, Bill 2, State 2, Category 2, Material 3, Rows 1 = **12**; move Apply/Reset to a `col-12` **right-aligned actions row** with `.filter-actions { display:flex; flex-direction:row; gap:8px; justify-content:flex-end; }` (higher specificity than `theme.css:273`, or use `:where()` there).
2. Keep the buttons at the 34 px filter height and make Apply primary, Reset secondary.
3. `Rows` select: `min-width:92px` (or move it into the actions row).
4. Remove the dead 100 option or raise the backend clamp.
5. **Tiles:** value `1.6rem`, padding 16, `min-height:96px`, one "Add" CTA; keep the header button as the only primary accent. This frees ~90 px above the table.
6. Card padding: 16 px for the filter body and tiles; 16 px table card top/bottom.

### Phase 6 — Cleanup & guardrails
- Delete duplicate `.ds-sales-table thead th` (`ui.css:849-853`).
- Delete the stale light-mode sale-form block (`theme.css:487-540`) or rewrite it against the classes that actually exist.
- Scope the theme-aware combobox to `.sale-form .combobox-list` and `.ds-filter-card .combobox-list` (keeps the rest of the app unchanged).
- Remove the ineffective `position:sticky` on the modal header/footer (`direct_sales.html:17-23`) — the body scrolls, not the header.
- Fix the systemic `background` shorthand in `.ui-form` (`ui.css:296`) and `.ui-v2` (`ui.css:485-490`) — this is 2 lines but affects 7 templates; ship it as a separate commit.
- Remove the attribute-selector hacks from `theme.css` only if the inline styles they target are removed in the same commit; otherwise leave them.
- Add a short "Sales CSS rules" comment block at the top of the sales style: token names, the item-row height, the breakpoint contract, and "do not re-declare `--ui-field-h`".

---

## 6. Verification matrix

| Check | 1440 | 1366 | 1280 | 1199 | 1024 | 991 | 768 | 390 |
|---|---|---|---|---|---|---|---|---|
| No horizontal scroll in the sheet | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Every item field visible & focusable | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| All boxes in one item row are equal height | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Filter actions under the inputs, side by side | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Table fits without scroll (8-col layout) | ✔ | ✔ | ✔ | — | — | — | cards | cards |
| Select chevron visible | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Pagination active state visible | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| Combobox readable (dark & light) | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ | ✔ |
| No clipped caption in mobile item rows | — | — | — | — | — | — | ✔ | ✔ |
| No stray border on the side panel | — | — | — | — | — | — | ✔ | ✔ |

Repeat every row in **dark** and **light**, and at **100 % / 125 %** browser zoom. Run `pytest` after each phase (the direct-sale tests cover booking/excess/alternate flows; there are no CSS tests, so visual checks are the guard).

---

## 7. Risks & guardrails

| Risk | Mitigation |
|---|---|
| `theme.css` is global; token edits affect every page | Keep the desktop values at the **currently computed** 40/34 → zero visual change; mobile stays 34/30. No global selector rewrites in Phase 1. |
| Global `.btn` rules use `!important` | Do not edit `.btn`/`.btn-sm` globally; scope all button sizing under `.sale-form` / `.ds-*`. |
| The Add and Edit sheets are duplicated markup | Any class/attribute change must be applied to both files; verify both modals in the matrix. |
| The table restructure (Option B) changes DOM used by scripts | Clicks use `data-edit-sale-url` and plain links; no JS depends on table column order. Check the Delete form and CSRF injection after the change. |
| Removing utility classes could reveal a specificity reliance | Remove them from **one** row first, screenshot, then sweep. |
| Light mode depends on `!important` chains | After each phase, toggle light mode; use a single `[data-theme="light"]` block per component, not per element. |

---

## 8. Confirmed decisions

| Decision | Choice |
|---|---|
| Sales table | **Option B** — 8 compact columns + kebab menu (Option A kept only as a rollback path) |
| Control scale | **40 / 34 / 30 px** (desktop / compact / mobile) |
| Shared fixes | **Allowed** — token de-duplication, select `background` shorthand, pagination highlight; one separate commit each |
| Form container | **Keep `modal-fullscreen`** — no drawer migration in this round |

**Execution order:** Phase 1 → 2 → 3 (form and filters), then Phase 4 (table markup), then Phase 5 → 6 (hierarchy + cleanup). Verify each phase against §6 in both themes before moving on. Phase 0 screenshots are the first action of Phase 1, captured from a running app rather than a headless browser (none is available in this environment).
