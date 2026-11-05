# Fixture real: Monza, 13 vueltas

- **Origen:** `captures/session-20261101-233816.f1cap`
- **SHA-256 del origen:** `D8F2F157A34B700F7951EB1621FEB656B8ED8874CEE2A73BB3688405D15F1A3C`
- **Archivo:** `monza-13-laps.f1cap.gz` (`5592587` bytes)
- **Comando:**

  ```powershell
  uv run f1-strategist trim captures\session-20261101-233816.f1cap tests\fixtures\real\monza-13-laps.f1cap.gz --types 1,2,3,4,7,8,10,11,12 --decimate 2=6 --decimate 7=6 --decimate 10=6 --decimate 11=2 --decimate 12=2 --laps 1-13 --anonymize