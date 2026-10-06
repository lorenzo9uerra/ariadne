# Changes from upstream

- Rebuilt the ELF for fresh flags, sizing its check array to the flag length and omitting unused checks. The key and character operations are unchanged.
- Adjusted the reference to read constants from the generated ELF and emit only the flag; its inversion is unchanged.
