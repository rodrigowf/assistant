/**
 * W-02 gallery sections: color roles, typography, shape/elevation/spacing, icons, and the
 * primitives (layout, ScrollArea, state layers, focus ring, spinner).
 */
import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { flushSync } from 'react-dom';
import { onThemeChange } from '@/styles';
import { filledIconPaths, iconNames, type FilledIconName, type IconName } from '@/ui/icons';
import {
  Box,
  Center,
  Cluster,
  FocusRing,
  Icon,
  Inline,
  ScrollArea,
  Spinner,
  Split,
  Stack,
  StateLayer,
  VisuallyHidden,
  typeClass,
  type ScrollAreaHandle,
  type TypeRoleClass,
} from '@/ui/primitives';
import type { GallerySection } from '../types';
import s from './primitives.gallery.module.css';

/* ------------------------------------------------------------------------------------------- */

function useThemeVersion(): number {
  const [v, setV] = useState(0);
  useEffect(
    () =>
      onThemeChange(() => {
        setV((n) => n + 1);
      }),
    [],
  );
  return v;
}

function cssValue(name: string): string {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  } catch {
    return '';
  }
}

function Board({ title, note, wide, children }: { title: string; note?: string; wide?: boolean; children: ReactNode }) {
  return (
    <Stack space="4" className={wide ? `${s.board} ${s.wide}` : s.board}>
      <h3 className={s.boardTitle}>
        {title}
        {note ? <small>{note}</small> : null}
      </h3>
      {children}
    </Stack>
  );
}

const sys = (role: string): string => `var(--md-sys-color-${role})`;

/* ---------- color roles ---------- */

function Swatch({ bg, fg, label, outline }: { bg: string; fg: string; label: string; outline?: boolean }) {
  useThemeVersion();
  const hex = cssValue(bg.slice(4, -1));
  return (
    <div className={outline ? `${s.swatch} ${s.swatchOutline}` : s.swatch} style={{ background: bg, color: fg }}>
      <span>{label}</span>
      <span className={s.hex}>{hex}</span>
    </div>
  );
}

const ACCENT = ['primary', 'secondary', 'tertiary', 'error'];

function ColorRoles() {
  return (
    <div className={s.grid}>
      <Board title="Accent roles" note="role / on-role · container / on-container" wide>
        <div className={s.pairs}>
          {ACCENT.flatMap((r) => [
            <div key={r} className={s.pair}>
              <Swatch bg={sys(r)} fg={sys(`on-${r}`)} label={r} />
              <Swatch bg={sys(`on-${r}`)} fg={sys(r)} label={`on-${r}`} />
            </div>,
            <div key={`${r}-c`} className={s.pair}>
              <Swatch bg={sys(`${r}-container`)} fg={sys(`on-${r}-container`)} label={`${r}-container`} />
              <Swatch bg={sys(`on-${r}-container`)} fg={sys(`${r}-container`)} label={`on-${r}-container`} />
            </div>,
          ])}
        </div>
      </Board>
      <Board title="Surfaces" note="separated by tone only, never by borders (R1)" wide>
        <div className={s.ladder}>
          {[
            'surface-container-lowest',
            'surface-dim',
            'surface',
            'surface-container-low',
            'surface-container',
            'surface-container-high',
            'surface-container-highest',
            'surface-bright',
          ].map((r) => (
            <div key={r} className={s.rung} style={{ background: sys(r) }}>
              <Stack space="1">
                <span>{r.replace('surface-container', 'container').replace('surface-', '')}</span>
                <span className={s.hex} style={{ color: sys('on-surface-variant') }}>
                  <HexOf role={r} />
                </span>
              </Stack>
            </div>
          ))}
        </div>
        <div className={s.swatches}>
          <Swatch bg={sys('surface-variant')} fg={sys('on-surface-variant')} label="surface-variant" />
          <Swatch bg={sys('inverse-surface')} fg={sys('inverse-on-surface')} label="inverse-surface" />
          <Swatch bg={sys('inverse-primary')} fg={sys('inverse-surface')} label="inverse-primary" />
          <Swatch bg={sys('on-surface')} fg={sys('surface')} label="on-surface" />
          <Swatch bg={sys('on-surface-variant')} fg={sys('surface')} label="on-surface-variant" />
          <Swatch bg={sys('outline')} fg={sys('surface')} label="outline" />
          <Swatch bg={sys('outline-variant')} fg={sys('on-surface')} label="outline-variant" />
        </div>
      </Board>
      <Board title="Semantic" note="extended colors: success · warning · info">
        <div className={s.pairs}>
          {['success', 'warning', 'info'].map((r) => (
            <div key={r} className={s.pair}>
              <Swatch bg={`var(--md-ext-color-${r})`} fg={`var(--md-ext-color-on-${r})`} label={r} />
              <Swatch bg={`var(--md-ext-color-${r}-container)`} fg={`var(--md-ext-color-on-${r}-container)`} label={`${r}-container`} />
              <Swatch bg={`var(--md-ext-color-${r}-tint)`} fg={`var(--md-ext-color-${r})`} label={`${r}-tint`} />
            </div>
          ))}
        </div>
      </Board>
      <Board title="State layers" note="on-surface over surface-container-highest">
        <Cluster space="2">
          {(['none', 'hover', 'focus', 'pressed', 'dragged'] as const).map((st) => (
            <div
              key={st}
              className={`${s.stateCell} ${s.highest} state-host`}
              data-state={st === 'none' ? undefined : st}
              style={{ width: 96 }}
            >
              {st === 'none' ? 'enabled' : st}
              <StateLayer />
            </div>
          ))}
          <div className={`${s.stateCell} ${s.disabled}`} style={{ width: 96 }}>
            disabled
          </div>
        </Cluster>
        <p className={s.note}>8 % hover · 10 % focus · 10 % pressed · 16 % dragged · 38 % / 12 % disabled content / container.</p>
      </Board>
      <Board title="Tool categories" note="tile = container / on-container · name = color · row = tint" wide>
        <div className={s.tools}>
          {TOOL_CATEGORIES.map(([cat, icon]) => (
            <div key={cat} className={s.toolRow} style={{ background: `var(--md-ext-color-tool-${cat}-tint)` }}>
              <span
                className={s.toolTile}
                style={{ background: `var(--md-ext-color-tool-${cat}-container)`, color: `var(--md-ext-color-on-tool-${cat}-container)` }}
              >
                <Icon name={icon} size={18} />
              </span>
              <span className={s.toolName} style={{ color: `var(--md-ext-color-tool-${cat})` }}>
                {cat}
              </span>
              <span className={s.toolMeta}>
                <HexOf name={`--md-ext-color-tool-${cat}`} />
              </span>
            </div>
          ))}
        </div>
      </Board>
    </div>
  );
}

function HexOf({ role, name }: { role?: string; name?: string }) {
  useThemeVersion();
  return <>{cssValue(name ?? `--md-sys-color-${role ?? ''}`)}</>;
}

const TOOL_CATEGORIES: [string, IconName][] = [
  ['read', 'description'],
  ['write', 'edit_document'],
  ['execute', 'terminal'],
  ['script', 'code'],
  ['navigate', 'explore'],
  ['capture', 'screenshot_monitor'],
  ['interact', 'touch_app'],
  ['todo', 'checklist'],
  ['task', 'assignment'],
  ['system', 'settings'],
  ['agent', 'smart_toy'],
  ['search', 'search'],
];

/* ---------- typography ---------- */

const TYPE_ROLES: [TypeRoleClass, string][] = [
  ['display-large', 'Archie'],
  ['display-medium', 'Good evening'],
  ['display-small', 'Living-room TV'],
  ['headline-large', 'Weekly energy report'],
  ['headline-medium', 'Delete this session?'],
  ['headline-small', 'Settings'],
  ['title-large', 'Refactor voice module'],
  ['title-medium', 'Open now · 3 sessions'],
  ['title-small', 'Archie (server)'],
  ['body-large', 'Dim the lights in the living room and put the news on the TV. Ação e informação também funcionam.'],
  ['body-medium', 'The agent process exited (code 0) after 41 turns. Memory files are kept.'],
  ['body-small', 'Port 8765 is added for you · 2 min ago'],
  ['label-large', 'Continue in new session'],
  ['label-medium', 'Listening 4 s'],
  ['label-small', 'CLAUDE · QWEN · GEMINI'],
];

function TypeRow({ role, sample }: { role: TypeRoleClass; sample: string }) {
  useThemeVersion();
  const v = (p: string): string => cssValue(`--md-sys-typescale-${role}-${p}`);
  const px = (rem: string): string => (rem.endsWith('rem') ? `${String(parseFloat(rem) * 16)}` : rem.replace('px', ''));
  return (
    <div className={s.typeRow}>
      <div className={s.typeMeta}>
        <span className={s.typeName}>{role}</span>
        <span className={s.typeSpec}>
          {px(v('size'))}/{px(v('line-height'))} · {v('weight')} · {v('tracking')}
        </span>
      </div>
      <p className={`${typeClass(role)} ${s.typeSample}`}>{sample}</p>
    </div>
  );
}

function Typography() {
  return (
    <div className={s.grid}>
      <Board title="Type scale" note="Roboto Flex (variable, wght 100–1000), M3 roles" wide>
        <div>
          {TYPE_ROLES.map(([role, sample]) => (
            <TypeRow key={role} role={role} sample={sample} />
          ))}
        </div>
      </Board>
      <Board title="Code" note="JetBrains Mono, loaded on first use">
        <TypeRow role="code" sample="ttl = settings.voice_token_ttl  // 0O 1lI {}[] => !=" />
        <pre className={`${typeClass('code')} ${s.codeBlock}`}>
          {'$ npm run build\nvite v8.3.2 building for production…\n✓ 412 modules transformed'}
        </pre>
      </Board>
      <Board title="Weights" note="one variable file serves every weight">
        <Stack space="1">
          {[300, 400, 500, 600, 700].map((w) => (
            <span key={w} className={typeClass('title-large')} style={{ fontWeight: w }}>
              {w} · Archie heard you
            </span>
          ))}
        </Stack>
      </Board>
    </div>
  );
}

/* ---------- shape, elevation, spacing ---------- */

const SHAPES = ['none', 'extra-small', 'small', 'medium', 'large', 'extra-large', 'full'];
const SPACES = ['1', '2', '3', '4', '5', '6', '8', '10', '12', '14', '16'];

function ShapeElevation() {
  return (
    <div className={s.grid}>
      <Board title="Shape" note="corner radius">
        <Cluster space="3">
          {SHAPES.map((k) => (
            <div key={k} className={s.shapeBox} style={{ borderRadius: `var(--md-sys-shape-corner-${k})` }}>
              {k}
            </div>
          ))}
        </Cluster>
      </Board>
      <Board title="Elevation" note="shadow + tonal surface per level">
        <Cluster space="4">
          {[0, 1, 2, 3, 4, 5].map((l) => (
            <div
              key={l}
              className={s.elevBox}
              style={{ background: `var(--md-sys-elevation-level${String(l)}-surface)`, boxShadow: `var(--md-sys-elevation-level${String(l)})` }}
            >
              level {l}
            </div>
          ))}
        </Cluster>
      </Board>
      <Board title="Spacing" note="4 dp grid, --app-space-N = N × 4 px">
        <Stack space="2">
          {SPACES.map((k) => (
            <Inline key={k} space="0">
              <span className={s.spaceKey}>{k}</span>
              <span className={s.spaceBar} style={{ width: `var(--app-space-${k})` }} />
            </Inline>
          ))}
        </Stack>
      </Board>
    </div>
  );
}

/* ---------- icons ---------- */

function Icons() {
  const filled = Object.keys(filledIconPaths) as FilledIconName[];
  return (
    <div className={s.grid}>
      <Board title="Icons" note={`${String(iconNames.length)} Material Symbols Rounded, weight 400, inline SVG`} wide>
        <div className={s.icons}>
          {iconNames.map((n) => (
            <div key={n} className={s.iconCell} title={n}>
              <Icon name={n} />
              <span className={s.iconName}>{n}</span>
            </div>
          ))}
        </div>
      </Board>
      <Board title="Filled variants" note="selected navigation, active toggles" wide>
        <div className={s.icons}>
          {filled.map((n) => (
            <div key={n} className={s.iconCell} title={`${n} (filled)`}>
              <Inline space="2">
                <Icon name={n} />
                <Icon name={n} filled style={{ color: 'var(--md-sys-color-primary)' }} />
              </Inline>
              <span className={s.iconName}>{n}</span>
            </div>
          ))}
        </div>
        <Inline space="4" className={s.note}>
          <Icon name="check_circle" size={16} />
          16
          <Icon name="check_circle" size={18} />
          18
          <Icon name="check_circle" size={20} />
          20
          <Icon name="check_circle" />
          24 px
        </Inline>
      </Board>
    </div>
  );
}

/* ---------- primitives ---------- */

function Block({ children, style }: { children?: ReactNode; style?: CSSProperties }) {
  return (
    <div className={s.block} style={style}>
      {children}
    </div>
  );
}

function IconBtn({ icon, label }: { icon: IconName; label: string }) {
  return (
    <button type="button" className={`${s.iconButton} has-state-layer`} aria-label={label}>
      <Icon name={icon} />
    </button>
  );
}

function Layouts() {
  return (
    <>
      <Board title="Stack" note="> * + * { margin-top }">
        <div className={s.demo}>
          <Stack space="2">
            <Block>space 2 (8 px)</Block>
            <Block>Conditional children that render null leave no gap</Block>
            {null}
            <Block>Third</Block>
          </Stack>
        </div>
      </Board>
      <Board title="Inline" note="icon + raw text, spaced (text-node trap)">
        <div className={s.demo}>
          <Stack space="3">
            <Inline space="2">
              <Icon name="terminal" size={18} />
              New agent session
            </Inline>
            <Inline space="3">
              <button type="button" className={`${s.btn} ${s.btnIcon} ${s.filled} has-state-layer`}>
                <Inline space="2" inline>
                  <Icon name="send" size={18} />
                  Send
                </Inline>
              </button>
              <button type="button" className={`${s.btn} ${s.tonal} has-state-layer`}>
                Save
              </button>
              <button type="button" className={`${s.btn} ${s.outlined} has-state-layer`}>
                Reject
              </button>
            </Inline>
          </Stack>
        </div>
      </Board>
      <Board title="Cluster" note="wrapping row, half-margin trick">
        <div className={s.demo}>
          <Cluster space="2" as="ul" aria-label="Suggestions">
            {['Plan the TV setup', 'Archie', 'Agents', 'Qwen', 'refactor.md', 'Weekly energy', 'Show on TV'].map((t, i) => (
              <li key={t}>
                <span className={i === 1 ? `${s.chip} ${s.chipSelected}` : s.chip}>
                  {i === 1 ? (
                    <Inline space="2" inline>
                      <Icon name="check" size={18} />
                      {t}
                    </Inline>
                  ) : (
                    t
                  )}
                </span>
              </li>
            ))}
          </Cluster>
        </div>
      </Board>
      <Board title="Split" note="trailing actions pushed to the end">
        <div className={s.demo}>
          <Split space="1">
            <span className={`${s.splitTitle} text-truncate`}>Refactor voice module</span>
            <Inline space="0">
              <IconBtn icon="search" label="Search" />
              <IconBtn icon="more_vert" label="More" />
            </Inline>
          </Split>
        </div>
      </Board>
      <Board title="Center and Box" note="max-width column + gutter · padding only">
        <div className={s.demo} style={{ padding: 0 }}>
          <Center max={280} gutter="4">
            <Box padding="3" style={{ background: 'var(--md-sys-color-surface-container-highest)', borderRadius: 12 }}>
              Center max 280 px, gutter 16 px; Box padding 12 px.
            </Box>
          </Center>
        </div>
      </Board>
    </>
  );
}

function ScrollDemo() {
  const area = useRef<ScrollAreaHandle>(null);
  const firstRef = useRef<HTMLDivElement>(null);
  const [first, setFirst] = useState(41);
  const [last, setLast] = useState(60);
  const [status, setStatus] = useState('');
  const update = (): void => {
    const a = area.current;
    if (a) setStatus(`near bottom: ${a.isNearBottom() ? 'yes' : 'no'} · ${String(Math.round(a.distanceFromBottom()))} px from bottom`);
  };
  useEffect(() => {
    void area.current?.scrollToBottom().then(update);
  }, []);
  const items: number[] = [];
  for (let i = first; i <= last; i += 1) items.push(i);
  return (
    <Board title="ScrollArea" note="momentum-safe scrollToBottom · preserveAnchor · isNearBottom">
      <ScrollArea ref={area} className={s.scrollBox} role="region" aria-label="Demo messages" tabIndex={0} onScroll={update}>
        {items.map((i) => (
          <div key={i} ref={i === first ? firstRef : undefined} className={i < 41 ? `${s.msg} ${s.msgNew}` : s.msg}>
            Message {i}
          </div>
        ))}
      </ScrollArea>
      <Cluster space="2">
        <button
          type="button"
          className={`${s.btn} ${s.tonal} has-state-layer`}
          disabled={first <= 1}
          onClick={() => {
            const anchor = firstRef.current;
            void area.current
              ?.preserveAnchor(
                () => {
                  flushSync(() => {
                    setFirst((f) => Math.max(1, f - 10));
                  });
                },
                { anchor },
              )
              .then(update);
          }}
        >
          Load 10 older
        </button>
        <button
          type="button"
          className={`${s.btn} ${s.outlined} has-state-layer`}
          onClick={() => {
            const stick = area.current?.isNearBottom() ?? false;
            flushSync(() => {
              setLast((l) => l + 1);
            });
            if (stick) void area.current?.scrollToBottom().then(update);
            else update();
          }}
        >
          Add message
        </button>
        <button
          type="button"
          className={`${s.btn} ${s.outlined} has-state-layer`}
          onClick={() => {
            void area.current?.scrollToBottom().then(update);
          }}
        >
          To bottom
        </button>
      </Cluster>
      <p className={s.status} aria-live="polite">
        {status}
      </p>
    </Board>
  );
}

function States() {
  const hosts: { cls: string | undefined; label: string }[] = [
    { cls: s.highest, label: 'surface' },
    { cls: s.filled, label: 'primary' },
    { cls: s.tonal, label: 'secondary-container' },
  ];
  return (
    <>
      <Board title="StateLayer" note="currentColor at the token opacity; forced with data-state">
        <Stack space="2">
          {hosts.map((h) => (
            <Inline key={h.label} space="2">
              {(['none', 'hover', 'focus', 'pressed'] as const).map((st) => (
                <div
                  key={st}
                  className={`${s.stateCell} ${h.cls ?? ''} state-host`}
                  data-state={st === 'none' ? undefined : st}
                  style={{ flex: '1 1 0', height: 44, fontSize: 12 }}
                >
                  {st === 'none' ? h.label.split('-')[0] : st}
                  <StateLayer />
                </div>
              ))}
            </Inline>
          ))}
        </Stack>
      </Board>
      <Board title="Focus ring, spinner, visually hidden" note="keyboard focus only (focus-visible)">
        <Stack space="4">
          <Inline space="4">
            <button type="button" className={`${s.btn} ${s.tonal} has-state-layer`}>
              Outline (default)
            </button>
            <span className={`${s.btn} ${s.tonal} focus-ring-host`} data-state="focus">
              FocusRing
              <FocusRing />
            </span>
          </Inline>
          <Inline space="4">
            <Spinner />
            <Spinner size={18} label="Working" />
            <Spinner size={24} />
            <span className={s.note}>14 · 18 · 24 px; keeps turning in low-end mode</span>
          </Inline>
          <p className={s.note}>
            Badge “3”
            <VisuallyHidden> (3 sessions need you)</VisuallyHidden> carries a screen-reader-only suffix.
          </p>
        </Stack>
      </Board>
    </>
  );
}

function Primitives() {
  return (
    <div className={s.grid}>
      <Layouts />
      <ScrollDemo />
      <States />
    </div>
  );
}

export const sections: GallerySection[] = [
  {
    id: 'colors',
    title: 'Color roles',
    description: 'M3 roles from apps/design-tokens (seed #879dc9, TonalSpot, pinned dark surfaces), plus semantic and tool-category extended colors. Hex values are read live from the active theme.',
    order: 10,
    Component: ColorRoles,
  },
  {
    id: 'typography',
    title: 'Typography',
    description: 'The M3 type scale in Roboto Flex, and the code style in JetBrains Mono. Both fonts are bundled locally (latin + latin-ext subsets).',
    order: 20,
    Component: Typography,
  },
  {
    id: 'shape',
    title: 'Shape, elevation, spacing',
    order: 30,
    Component: ShapeElevation,
  },
  {
    id: 'icons',
    title: 'Icons',
    description: 'Generated from src/ui/icons/icons.manifest.json by npm run icons. Same names as the Android app.',
    order: 40,
    Component: Icons,
  },
  {
    id: 'primitives',
    title: 'Primitives',
    description: 'Layout without flex gap (Safari 12), the ScrollArea every scroller uses, and the interaction primitives.',
    order: 50,
    Component: Primitives,
  },
];
