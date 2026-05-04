/** Platform-aware keybinding helpers.
 *
 * On macOS the "action" modifier is Cmd. Modern terminals that support kitty
 * keyboard protocol report Cmd as `key.super`; legacy terminals often surface it
 * as `key.meta`. Some macOS terminals also translate Cmd+Left/Right/Backspace
 * into readline-style Ctrl+A/Ctrl+E/Ctrl+U before the app sees them.
 * On other platforms the action modifier is Ctrl.
 * Ctrl+C stays the interrupt key on macOS. On non-mac terminals it can also
 * copy an active TUI selection, matching common terminal selection behavior.
 */

export const isMac = process.platform === 'darwin'

/** True when the platform action-modifier is pressed (Cmd on macOS, Ctrl elsewhere). */
export const isActionMod = (key: { ctrl: boolean; meta: boolean; super?: boolean }): boolean =>
  isMac ? key.meta || key.super === true : key.ctrl

/**
 * Accept raw Ctrl+<letter> as an action shortcut on macOS, where `isActionMod`
 * otherwise means Cmd. Two motivations:
 *   - Some macOS terminals rewrite Cmd navigation/deletion into readline control
 *     keys (Cmd+Left → Ctrl+A, Cmd+Right → Ctrl+E, Cmd+Backspace → Ctrl+U).
 *   - Ctrl+K (kill-to-end) and Ctrl+W (delete-word-back) are standard readline
 *     bindings that users expect to work regardless of platform, even though
 *     no terminal rewrites Cmd into them.
 */
export const isMacActionFallback = (
  key: { ctrl: boolean; meta: boolean; super?: boolean },
  ch: string,
  target: 'a' | 'e' | 'u' | 'k' | 'w'
): boolean => isMac && key.ctrl && !key.meta && key.super !== true && ch.toLowerCase() === target

/** Match action-modifier + a single character (case-insensitive). */
export const isAction = (key: { ctrl: boolean; meta: boolean; super?: boolean }, ch: string, target: string): boolean =>
  isActionMod(key) && ch.toLowerCase() === target

export const isRemoteShell = (env: NodeJS.ProcessEnv = process.env): boolean =>
  Boolean(env.SSH_CONNECTION || env.SSH_CLIENT || env.SSH_TTY)

export const isCopyShortcut = (
  key: { ctrl: boolean; meta: boolean; super?: boolean },
  ch: string,
  env: NodeJS.ProcessEnv = process.env
): boolean =>
  ch.toLowerCase() === 'c' &&
  (isAction(key, ch, 'c') ||
    (isRemoteShell(env) && (key.meta || key.super === true)) ||
    // VS Code/Cursor/Windsurf terminal setup forwards Cmd+C as a CSI-u
    // sequence with the super bit plus a benign ctrl bit. Accept that shape
    // even though raw Ctrl+C should remain interrupt on local macOS.
    (isMac && key.ctrl && (key.meta || key.super === true)))

/**
 * Voice recording toggle key — configurable via ``voice.record_key`` in
 * ``config.yaml`` (default ``ctrl+b``).
 *
 * Documented in tips.py, the Python CLI prompt_toolkit handler, and the
 * config.yaml default. The TUI honours the same config knob (#18994);
 * when ``voice.record_key`` is e.g. ``ctrl+o`` the TUI binds Ctrl+O.
 *
 * On macOS we additionally accept the platform action modifier (Cmd) for
 * the configured letter so existing macOS muscle memory keeps working
 * alongside the documented Ctrl+<letter> shortcut.
 */
export type VoiceRecordKeyMod = 'alt' | 'ctrl' | 'super'

/** Named (multi-character) keys we support, matching the CLI's
 * prompt_toolkit binding shape (``c-space``, ``c-enter``, etc.) so a
 * config value like ``ctrl+space`` binds in both runtimes. */
export type VoiceRecordKeyNamed = 'backspace' | 'delete' | 'enter' | 'escape' | 'space' | 'tab'

export interface ParsedVoiceRecordKey {
  /** Single character (``'b'``, ``'o'``) when ``named`` is undefined,
   * otherwise the named-key token (``'space'``, ``'enter'``…). Kept as
   * one field for back-compat with the v1 ``{ ch, mod, raw }`` shape. */
  ch: string
  mod: VoiceRecordKeyMod
  named?: VoiceRecordKeyNamed
  raw: string
}

export const DEFAULT_VOICE_RECORD_KEY: ParsedVoiceRecordKey = {
  ch: 'b',
  mod: 'ctrl',
  raw: 'ctrl+b'
}

/** Modifier aliases. ``meta`` is intentionally absent: hermes-ink sets
 * ``key.meta`` for plain Alt/Option (and on some legacy terminals for Cmd
 * too), so accepting a literal ``meta+b`` config would display as ``Cmd+B``
 * but match Alt+B on the wire — the kind of lie this fix was meant to
 * remove. Users who want the platform action modifier spell it ``cmd`` /
 * ``command`` / ``super`` / ``win``. */
const _MOD_ALIASES: Record<string, VoiceRecordKeyMod> = {
  alt: 'alt',
  cmd: 'super',
  command: 'super',
  control: 'ctrl',
  ctrl: 'ctrl',
  option: 'alt',
  opt: 'alt',
  super: 'super',
  win: 'super',
  windows: 'super'
}

/** Map config-string named tokens to the canonical name used at match time.
 *
 * Aliases mirror what prompt_toolkit accepts (``return`` ↔ ``enter``,
 * ``esc`` ↔ ``escape``) so a config that round-trips through the CLI also
 * binds in the TUI. */
const _NAMED_KEY_ALIASES: Record<string, VoiceRecordKeyNamed> = {
  backspace: 'backspace',
  bs: 'backspace',
  del: 'delete',
  delete: 'delete',
  enter: 'enter',
  esc: 'escape',
  escape: 'escape',
  ret: 'enter',
  return: 'enter',
  space: 'space',
  spc: 'space',
  tab: 'tab'
}

interface RuntimeKeyEvent {
  alt?: boolean
  backspace?: boolean
  ctrl: boolean
  delete?: boolean
  escape?: boolean
  meta: boolean
  return?: boolean
  super?: boolean
  tab?: boolean
}

/** Match an ink ``key`` event against a parsed named key. The ink runtime
 * sets one boolean per named key; ``space`` is a printable char so it
 * arrives as ``ch === ' '`` rather than a dedicated ``key.space`` flag. */
const _matchesNamedKey = (
  named: VoiceRecordKeyNamed,
  key: RuntimeKeyEvent,
  ch: string
): boolean => {
  switch (named) {
    case 'backspace':
      return key.backspace === true
    case 'delete':
      return key.delete === true
    case 'enter':
      return key.return === true
    case 'escape':
      return key.escape === true
    case 'space':
      return ch === ' '
    case 'tab':
      return key.tab === true
  }
}

/**
 * Parse a config-string voice record key like ``ctrl+b`` / ``alt+r`` /
 * ``ctrl+space`` into ``{mod, ch, named?}``. Accepts single characters
 * AND the named tokens declared in ``_NAMED_KEY_ALIASES`` (``space``,
 * ``enter``/``return``, ``tab``, ``escape``/``esc``, ``backspace``,
 * ``delete``) — matching the keys prompt_toolkit accepts on the CLI
 * side via the ``c-<name>`` rewrite in ``cli.py``.
 *
 * Falls back to the documented Ctrl+B default for empty input or for
 * unrecognised multi-character tokens so a typo never silently disables
 * the shortcut.
 */
export const parseVoiceRecordKey = (raw: string): ParsedVoiceRecordKey => {
  const lower = (raw ?? '').trim().toLowerCase()

  if (!lower) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const parts = lower.split('+').map(p => p.trim()).filter(Boolean)

  if (!parts.length) {
    return DEFAULT_VOICE_RECORD_KEY
  }

  const last = parts[parts.length - 1]
  const modCandidates = parts.slice(0, -1)

  let mod: VoiceRecordKeyMod = 'ctrl'

  if (modCandidates.length) {
    const norm = _MOD_ALIASES[modCandidates[0]]

    // Unknown modifier token (e.g. bare ``meta+b`` which is ambiguous on
    // the wire) falls back to the documented default rather than
    // silently coercing to Ctrl and producing a misleading bind.
    if (!norm) {
      return DEFAULT_VOICE_RECORD_KEY
    }

    mod = norm
  }

  if (last.length === 1) {
    return { ch: last, mod, raw: lower }
  }

  const named = _NAMED_KEY_ALIASES[last]

  if (named) {
    return { ch: named, mod, named, raw: lower }
  }

  // Unknown multi-character token (e.g. typo'd ``ctrl+spcae``) — fall back
  // to the doc default rather than silently disabling the binding.
  return DEFAULT_VOICE_RECORD_KEY
}

/** Render a parsed key back as ``Ctrl+B`` / ``Ctrl+Space`` for status text. */
export const formatVoiceRecordKey = (parsed: ParsedVoiceRecordKey): string => {
  const modLabel = parsed.mod === 'super' ? 'Cmd' : parsed.mod[0].toUpperCase() + parsed.mod.slice(1)
  // Named tokens render in title case (Ctrl+Space, Ctrl+Enter); single
  // chars render upper-case to match the existing Ctrl+B convention.
  const keyLabel = parsed.named
    ? parsed.named[0].toUpperCase() + parsed.named.slice(1)
    : parsed.ch.toUpperCase()

  return `${modLabel}+${keyLabel}`
}

const _isDefaultVoiceKey = (parsed: ParsedVoiceRecordKey): boolean =>
  parsed.raw === DEFAULT_VOICE_RECORD_KEY.raw

export const isVoiceToggleKey = (
  key: RuntimeKeyEvent,
  ch: string,
  configured: ParsedVoiceRecordKey = DEFAULT_VOICE_RECORD_KEY
): boolean => {
  // Match the configured key first (single-char compare or named-key
  // event-property check). Bail out before evaluating modifier shape
  // so the wrong key never reaches the modifier guard.
  if (configured.named) {
    if (!_matchesNamedKey(configured.named, key, ch)) {
      return false
    }
  } else if (ch.toLowerCase() !== configured.ch) {
    return false
  }

  switch (configured.mod) {
    case 'alt':
      // Most terminals surface Alt as either ``alt`` or ``meta``; accept
      // both so the binding works across xterm-style and kitty-style
      // protocols. Guard against ctrl/super bits so a chord like
      // Ctrl+Alt+<key> or Cmd+Alt+<key> doesn't spuriously fire the
      // alt binding.
      return (key.alt === true || key.meta) && !key.ctrl && key.super !== true
    case 'ctrl':
      // Only the documented default (``ctrl+b``) gets the macOS
      // Cmd-as-action-modifier fallback. For any user-configured binding
      // we require the literal Ctrl bit — otherwise ``ctrl+escape`` would
      // fire on bare Esc (hermes-ink sets ``key.meta`` for bare Esc on
      // some macOS terminals) and ``ctrl+space`` would fire on Alt+Space.
      return key.ctrl || (_isDefaultVoiceKey(configured) && isActionMod(key))
    case 'super':
      // Kitty-style protocol surfaces Cmd as ``key.super``; legacy macOS
      // terminals still surface it as ``key.meta``. Accept both but
      // require the ctrl bit to be clear so Ctrl+Cmd+<key> doesn't match.
      return (key.super === true || (isMac && key.meta)) && !key.ctrl
  }
}
