# Why the RHDN release of "DQ5 - 4 Party Members" does not work

This document records the diagnosis behind the corrected patch in this
repository. Short version: the released DeJap conversion is the original
Japanese patch **shifted 512 bytes**, so it writes every edit into the wrong
place and never installs the feature at all.

None of this reflects on the original hack. Mr. 45's 2007 work is sound and
applies cleanly. The fault is entirely in the 2022 conversion step, and it is
the single easiest mistake to make in SNES romhacking.

---

## The reported failure

Multiple users, separately sourced ROMs, three emulators (bsnes, Mesen,
snes9x), one identical signature:

```
boots -> title screen -> name entry (accepted) -> message speed prompt
      -> stereo/mono prompt -> hang, with a door/chest-open sound effect
```

The sound effect is the diagnostic tell. A game handling an error condition
does not play a door-open SFX. The CPU is executing data as code and has
wandered into an arbitrary sound call.

---

## Establishing the baseline

Correct build chain for the DeJap translation, every stage verified by CRC32:

| Stage | Size | CRC32 |
|---|---|---|
| Japanese base ROM, headerless | 1,572,864 | `BC955F3B` |
| + 512-byte copier header | 1,573,376 | `8E637962` |
| + DeJap v2.01 IPS | 2,097,664 | `A2E6F36A` |
| - header stripped | 2,097,152 | `9400CB3C` |

`9400CB3C` is exactly the source CRC that AustNerevar's own BPS declares, which
confirms the chain is right and that his patch was built against a correctly
translated ROM. The mapping is **LoROM** (internal header at `0x7FC0`, scored
62 against 18 for HiROM: printable title `DRAGONQUEST5`, mapmode `0x20`,
checksum and complement XOR to `FFFF`, RESET vector `$8639` in range).

So the inputs were never the problem.

---

## The evidence

Mr. 45's original IPS changes **1210 byte positions** across **212 regions**.
AustNerevar's conversion changes **1596 bytes** across **205 regions**.
Comparing the two change sets:

| Test | Result |
|---|---|
| Mr. 45's new byte value also written by AustNerevar at the **same** offset | 2 / 1210 (0.2%) |
| ...at offset **+0x200** | **1140 / 1210 (94.2%)** |
| ...at offset **-0x200** | 0 / 1210 (0.0%) |

94% of the payload appears 512 bytes late. And critically, the bytes being
overwritten at those destinations are unrelated: original-byte agreement at
`+0x200` is only 69 / 1210. The patch is writing correct data onto wrong code.

### A concrete example

Mr. 45 retargets the party data table at file offset `0x004322`
(LoROM `$00:C322`), bumping the table index to make room for a fourth member:

```
JP base :  8d e6 32      STA $32E6
Mr. 45  :  8d e7 32      STA $32E7
```

The DeJap ROM contains that identical code at that identical offset. The
conversion leaves it completely untouched, and instead writes 512 bytes later,
into an unrelated routine:

```
offset 0x00452A    a6 c1   LDX $C1   ->   a6 e7   LDX $E7
offset 0x004535    a5 04   LDA $04   ->   a5 87   LDA $87
```

Those two instructions had nothing to do with party size. Their operands are
now Mr. 45's data bytes, and the actual target code still says `STA $32E6`.

### Consequences

1. **The feature is never installed.** Every real target site is left alone.
2. **~1600 bytes of working code are corrupted** at scattered offsets.

Which is precisely the observed behaviour. Boot, title, name entry and the
config prompts all run because none of that code was hit. The first execution
of a corrupted region comes when new-game party initialisation runs, right
after the config prompts, and the CPU derails into garbage.

### How the mistake happens

Mr. 45's IPS is written against a **headerless** ROM. This is provable from his
own notes, which cite ROM offsets `0x017FE8`, `00C066`, `00C076` and `00C0FA`;
the headerless base ROM holds exactly the bytes he documents at those addresses
(`a2 00 78`, `a2 00 7c`, `a2 80 01`), and his patch covers `0x017FE8`.

Apply a headerless-targeted IPS to a **headered** ROM and every write lands 512
bytes late. Diff that against a headerless ROM to produce a distributable
patch, and the 512-byte error is baked in permanently. IPS has no source
validation, so nothing complains at any point.

---

## The correction

DeJap expanded the ROM from 1.5 MB to 2 MB, but it appended translated text
rather than relocating this code. That makes the fix straightforward:

- **1204 of Mr. 45's 1210 patch sites (99.5%) are byte-identical between the
  Japanese base ROM and the DeJap ROM.**
- 205 of 212 regions are identical including 8 bytes of surrounding context.

So the overwhelming majority of the patch applies directly at its correct
offsets, no rebasing required. Seven regions in bank `$04` needed judgement:

| Site | Nature | Action |
|---|---|---|
| `0x027FF0` | 14-byte menu descriptor Mr. 45 parks in free space | **applied** - the space is zero-filled in both ROMs |
| `0x026BD2` | pointer redirected to `$FFF0` | omitted |
| `0x026D5C`, `0x026D80`, `0x027265`, `0x027278` | row counts, +2 | omitted |
| `0x0272F7` | row count, -2 | omitted |

Those six sites are stats-menu layout values tuned to the *Japanese* menu.
DeJap rebuilt those menus for English text, so their values differ and Mr. 45's
Japanese-layout numbers do not transfer. They are omitted, and the stats menu
renders correctly without them - DeJap's own layout work already accommodates
the display.

The result is **206 of 212 regions applied**, then the SNES internal checksum
recomputed.

### Verification of the result

- Overwrites **zero** bytes that DeJap had changed - the omitted sites retain
  DeJap's values, confirmed byte by byte.
- Differs from the plain DeJap ROM at exactly 1207 positions: 1203 patch bytes
  plus the 4-byte checksum field.
- Internal checksum `387F` / complement `C780`, verified correct. The image is
  2 MB, a power of two, so the non-power-of-two mirroring rule does not apply
  here (it does for the 1.5 MB Japanese ROM, whose stored `BAF9` this same
  routine reproduces exactly - a naive full-image sum gives `AB17`).

---

## Note on the checksum trap

Worth recording for anyone rebuilding this. The Japanese base ROM is 1.5 MB,
not a power of two. The SNES stored checksum for such an image is not a plain
sum of all bytes: the image splits into the largest power-of-two prefix (1 MB)
summed normally, plus the 0.5 MB remainder summed and doubled to simulate
mirroring. Getting this wrong yields a ROM that may still boot while failing
checksum validation on accurate emulators and hardware.

The implementation in `build.py` reproduces the untouched Japanese ROM's own
stored checksum (`BAF9`) exactly, which is the only honest way to know it is
correct before trusting it on a patched image.

---

## Reproducing this analysis

Everything above is static comparison of four ROM images - the Japanese base,
the DeJap build, Mr. 45's Japanese 4-party build, and AustNerevar's DeJap
4-party build - plus the two patch files. No emulator is required to confirm
the `+0x200` finding; it falls out of a byte-level diff of the two change sets.
