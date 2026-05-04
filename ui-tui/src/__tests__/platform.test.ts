import { afterEach, describe, expect, it, vi } from 'vitest'

const originalPlatform = process.platform

async function importPlatform(platform: NodeJS.Platform) {
  vi.resetModules()
  Object.defineProperty(process, 'platform', { value: platform })

  return import('../lib/platform.js')
}

afterEach(() => {
  Object.defineProperty(process, 'platform', { value: originalPlatform })
  vi.resetModules()
})

describe('platform action modifier', () => {
  it('treats kitty Cmd sequences as the macOS action modifier', async () => {
    const { isActionMod } = await importPlatform('darwin')

    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: true, super: false })).toBe(true)
    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(false)
  })

  it('still uses Ctrl as the action modifier on non-macOS', async () => {
    const { isActionMod } = await importPlatform('linux')

    expect(isActionMod({ ctrl: true, meta: false, super: false })).toBe(true)
    expect(isActionMod({ ctrl: false, meta: false, super: true })).toBe(false)
  })
})

describe('isCopyShortcut', () => {
  it('keeps Ctrl+C as the local non-macOS copy chord', async () => {
    const { isCopyShortcut } = await importPlatform('linux')

    expect(isCopyShortcut({ ctrl: true, meta: false, super: false }, 'c', {})).toBe(true)
  })

  it('accepts client Cmd+C over SSH even when running on Linux', async () => {
    const { isCopyShortcut } = await importPlatform('linux')
    const env = { SSH_CONNECTION: '1 2 3 4' } as NodeJS.ProcessEnv

    expect(isCopyShortcut({ ctrl: false, meta: false, super: true }, 'c', env)).toBe(true)
    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', env)).toBe(true)
  })

  it('does not treat local Linux Alt+C as copy', async () => {
    const { isCopyShortcut } = await importPlatform('linux')

    expect(isCopyShortcut({ ctrl: false, meta: true, super: false }, 'c', {})).toBe(false)
  })

  it('accepts the VS Code/Cursor forwarded Cmd+C copy sequence on macOS', async () => {
    const { isCopyShortcut } = await importPlatform('darwin')

    expect(isCopyShortcut({ ctrl: true, meta: false, super: true }, 'c', {})).toBe(true)
  })
})

describe('isVoiceToggleKey', () => {
  it('matches raw Ctrl+B on macOS (doc-default across platforms)', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'B')).toBe(true)
  })

  it('matches Cmd+B on macOS (preserve platform muscle memory)', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b')).toBe(true)
  })

  it('matches Ctrl+B on non-macOS platforms', async () => {
    const { isVoiceToggleKey } = await importPlatform('linux')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
  })

  it('does not match unmodified b or other Ctrl combos', async () => {
    const { isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, 'b')).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'a')).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'c')).toBe(false)
  })
})

describe('parseVoiceRecordKey (#18994)', () => {
  it('falls back to Ctrl+B for empty input', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('parses ctrl+<letter> bindings', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('ctrl+o')).toEqual({ ch: 'o', mod: 'ctrl', raw: 'ctrl+o' })
    expect(parseVoiceRecordKey('Ctrl+R')).toEqual({ ch: 'r', mod: 'ctrl', raw: 'ctrl+r' })
  })

  it('parses alt/cmd/super aliases', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    expect(parseVoiceRecordKey('alt+b').mod).toBe('alt')
    expect(parseVoiceRecordKey('option+b').mod).toBe('alt')
    // ``cmd``/``command`` collapse onto ``super`` — hermes-ink's ``key.meta``
    // means Alt on most terminals, so mapping ``cmd`` to a distinct ``meta``
    // mod would make the display lie about the actual match target.
    expect(parseVoiceRecordKey('cmd+b').mod).toBe('super')
    expect(parseVoiceRecordKey('command+b').mod).toBe('super')
    expect(parseVoiceRecordKey('super+b').mod).toBe('super')
    expect(parseVoiceRecordKey('win+b').mod).toBe('super')
  })

  it('treats a bare "meta+b" as unrecognised (falls back to Ctrl+B)', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // ``meta`` is ambiguous on the wire (Alt on xterm, Cmd on legacy
    // macOS). Rather than pick the wrong one, refuse it.
    expect(parseVoiceRecordKey('meta+b')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })

  it('parses named keys (space, enter, tab, escape, backspace, delete)', async () => {
    const { parseVoiceRecordKey } = await importPlatform('linux')

    // Every named token from the CLI's prompt_toolkit ``c-<name>`` set is
    // accepted with both the canonical name and its common alias.
    expect(parseVoiceRecordKey('ctrl+space')).toEqual({
      ch: 'space',
      mod: 'ctrl',
      named: 'space',
      raw: 'ctrl+space'
    })
    expect(parseVoiceRecordKey('alt+enter').named).toBe('enter')
    expect(parseVoiceRecordKey('alt+return').named).toBe('enter') // ``return`` ↔ ``enter``
    expect(parseVoiceRecordKey('ctrl+tab').named).toBe('tab')
    expect(parseVoiceRecordKey('ctrl+escape').named).toBe('escape')
    expect(parseVoiceRecordKey('ctrl+esc').named).toBe('escape') // ``esc`` alias
    expect(parseVoiceRecordKey('ctrl+backspace').named).toBe('backspace')
    expect(parseVoiceRecordKey('ctrl+delete').named).toBe('delete')
    expect(parseVoiceRecordKey('ctrl+del').named).toBe('delete') // ``del`` alias
  })

  it('falls back to Ctrl+B for unrecognised multi-character tokens', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, parseVoiceRecordKey } = await importPlatform('linux')

    // Typos / unsupported names (``ctrl+spcae``, ``ctrl+f5``, …) fall back
    // to the documented Ctrl+B default rather than silently disabling the
    // binding.
    expect(parseVoiceRecordKey('ctrl+spcae')).toEqual(DEFAULT_VOICE_RECORD_KEY)
    expect(parseVoiceRecordKey('ctrl+f5')).toEqual(DEFAULT_VOICE_RECORD_KEY)
  })
})

describe('formatVoiceRecordKey (#18994)', () => {
  it('renders as the user expects in /voice status', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+b'))).toBe('Ctrl+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+o'))).toBe('Ctrl+O')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('alt+r'))).toBe('Alt+R')
    // ``cmd``/``command``/``super``/``win`` all collapse onto the super
    // modifier and render as ``Super`` on non-mac so the hint doesn't
    // tell Linux/Windows users to press a Cmd key they don't have.
    expect(formatVoiceRecordKey(parseVoiceRecordKey('cmd+b'))).toBe('Super+B')
  })

  it('renders named keys in title case (Ctrl+Space, Ctrl+Enter)', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+space'))).toBe('Ctrl+Space')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('alt+enter'))).toBe('Alt+Enter')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('ctrl+esc'))).toBe('Ctrl+Escape')
  })
})

describe('isVoiceToggleKey honours configured record key (#18994)', () => {
  it('binds the configured letter, not hardcoded b', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
    // The old hardcoded 'b' must NOT match when the user configured 'o'.
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', ctrlO)).toBe(false)
  })

  it('alt+<letter> binding matches alt OR meta (terminal-protocol parity)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')
    const altR = parseVoiceRecordKey('alt+r')

    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: false }, 'r', altR)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'r', altR)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, 'r', altR)).toBe(false)
  })

  it('binds named keys via ink event flags (space → ch === " ", enter → key.return, …)', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('linux')

    const ctrlSpace = parseVoiceRecordKey('ctrl+space')
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, ' ', ctrlSpace)).toBe(true)
    // Single-char ``b`` must NOT match a ``space``-configured binding.
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', ctrlSpace)).toBe(false)
    // Space without the configured modifier must not fire either.
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: false }, ' ', ctrlSpace)).toBe(false)

    const ctrlEnter = parseVoiceRecordKey('ctrl+enter')
    expect(isVoiceToggleKey({ ctrl: true, meta: false, return: true, super: false }, '', ctrlEnter)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, return: false, super: false }, '', ctrlEnter)).toBe(false)

    const altTab = parseVoiceRecordKey('alt+tab')
    expect(isVoiceToggleKey({ alt: true, ctrl: false, meta: false, super: false, tab: true }, '', altTab)).toBe(true)
    expect(isVoiceToggleKey({ alt: false, ctrl: false, meta: false, super: false, tab: true }, '', altTab)).toBe(false)

    const ctrlEscape = parseVoiceRecordKey('ctrl+escape')
    expect(isVoiceToggleKey({ ctrl: true, escape: true, meta: false, super: false }, '', ctrlEscape)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, escape: false, meta: false, super: false }, '', ctrlEscape)).toBe(false)

    const ctrlBackspace = parseVoiceRecordKey('ctrl+backspace')
    expect(isVoiceToggleKey({ backspace: true, ctrl: true, meta: false, super: false }, '', ctrlBackspace)).toBe(true)

    const ctrlDelete = parseVoiceRecordKey('ctrl+delete')
    expect(isVoiceToggleKey({ ctrl: true, delete: true, meta: false, super: false }, '', ctrlDelete)).toBe(true)
  })

  it('omitted configured key falls back to ctrl+b (back-compat)', async () => {
    const { isVoiceToggleKey } = await importPlatform('linux')

    // No third arg → DEFAULT_VOICE_RECORD_KEY → Ctrl+B behaviour.
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b')).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o')).toBe(false)
  })

  // Regressions from Copilot review on #19835: the previous implementation
  // accepted ``isActionMod(key)`` in the ``ctrl`` branch for every
  // configured key, so bare Esc (which hermes-ink reports with
  // ``key.meta`` on some macOS terminals) fired ``ctrl+escape``, and
  // Alt+Space / Alt+Tab fired ``ctrl+space`` / ``ctrl+tab``. The fallback
  // is now gated to the documented default (``ctrl+b``) only.
  it('ctrl+escape does NOT fire on bare Esc via key.meta on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlEscape = parseVoiceRecordKey('ctrl+escape')

    // Bare Esc on a legacy macOS terminal: ``key.meta: true``, ``key.escape: true``, no ctrl.
    expect(isVoiceToggleKey({ ctrl: false, escape: true, meta: true, super: false }, '', ctrlEscape)).toBe(false)
    // Real Ctrl+Esc still fires.
    expect(isVoiceToggleKey({ ctrl: true, escape: true, meta: false, super: false }, '', ctrlEscape)).toBe(true)
  })

  it('ctrl+space does NOT fire on Alt+Space on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlSpace = parseVoiceRecordKey('ctrl+space')

    // Alt+Space surfaces as ``key.meta: true`` with space char.
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, ' ', ctrlSpace)).toBe(false)
    // Real Ctrl+Space still fires.
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, ' ', ctrlSpace)).toBe(true)
  })

  it('default ctrl+b still accepts Cmd+B on macOS (muscle-memory preserved)', async () => {
    const { DEFAULT_VOICE_RECORD_KEY, isVoiceToggleKey } = await importPlatform('darwin')

    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'b', DEFAULT_VOICE_RECORD_KEY)).toBe(true)
  })

  it('custom ctrl+<letter> does NOT accept Cmd fallback on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const ctrlO = parseVoiceRecordKey('ctrl+o')

    // Only ``ctrl+b`` gets the action-modifier fallback; ``ctrl+o`` must
    // be a literal Ctrl bit — otherwise Cmd+O would steal the shortcut.
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'o', ctrlO)).toBe(false)
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', ctrlO)).toBe(true)
  })

  it('cmd+b renders "Cmd+B" and matches key.super OR (macOS) key.meta', async () => {
    const { formatVoiceRecordKey, isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const cmdB = parseVoiceRecordKey('cmd+b')

    expect(formatVoiceRecordKey(cmdB)).toBe('Cmd+B')
    // Kitty-style: key.super
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', cmdB)).toBe(true)
    // Legacy macOS terminal: key.meta
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', cmdB)).toBe(true)
    // Ctrl held at the same time → reject (different chord).
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: true }, 'b', cmdB)).toBe(false)
  })

  // Round-2 Copilot review regressions on #19835.
  it('super+b renders "Super+B" on Linux (not "Cmd+B")', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('linux')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Super+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('win+b'))).toBe('Super+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('cmd+b'))).toBe('Super+B')
  })

  it('super+b still renders "Cmd+B" on macOS', async () => {
    const { formatVoiceRecordKey, parseVoiceRecordKey } = await importPlatform('darwin')

    expect(formatVoiceRecordKey(parseVoiceRecordKey('super+b'))).toBe('Cmd+B')
    expect(formatVoiceRecordKey(parseVoiceRecordKey('win+b'))).toBe('Cmd+B')
  })

  it('ctrl+b aliases (control+b, "ctrl + b") still accept Cmd+B fallback on macOS', async () => {
    const { isVoiceToggleKey, parseVoiceRecordKey } = await importPlatform('darwin')
    const controlB = parseVoiceRecordKey('control+b')
    const spacedB = parseVoiceRecordKey('ctrl + b')

    // Both parse to the documented default semantically; both must keep
    // the macOS Cmd+B muscle-memory fallback.
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', controlB)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: true, super: false }, 'b', spacedB)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', controlB)).toBe(true)
    expect(isVoiceToggleKey({ ctrl: false, meta: false, super: true }, 'b', spacedB)).toBe(true)
    // And still reject a ctrl bit on a different letter.
    expect(isVoiceToggleKey({ ctrl: true, meta: false, super: false }, 'o', controlB)).toBe(false)
  })
})

describe('isMacActionFallback', () => {
  it('routes raw Ctrl+K and Ctrl+W to readline kill-to-end / delete-word on macOS', async () => {
    const { isMacActionFallback } = await importPlatform('darwin')

    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(true)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(true)
    // Must not fire when Cmd (meta/super) is held — those are distinct chords.
    expect(isMacActionFallback({ ctrl: true, meta: true, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: true }, 'w', 'w')).toBe(false)
  })

  it('is a no-op on non-macOS (Linux routes Ctrl+K/W through isActionMod directly)', async () => {
    const { isMacActionFallback } = await importPlatform('linux')

    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'k', 'k')).toBe(false)
    expect(isMacActionFallback({ ctrl: true, meta: false, super: false }, 'w', 'w')).toBe(false)
  })
})
