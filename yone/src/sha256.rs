//! Pure-Rust SHA-256 (FIPS 180-4). Verified against the NIST test vectors
//! in the unit tests below and against coreutils `sha256sum` in the e2e suite.
//!
//! On x86-64 processors with the SHA extensions (most made since 2017-2019),
//! blocks go through the SHA-NI instructions, chosen at run time: about
//! eight times the speed of the portable code, which every other processor
//! uses. Both give the same digests (tested).

use std::fs::File;
use std::io::{self, Read, Write};
use std::path::Path;

const K: [u32; 64] = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
];

#[derive(Clone)]
pub struct Sha256 {
    h: [u32; 8],
    buf: [u8; 64],
    buflen: usize,
    len: u64,
}

impl Default for Sha256 {
    fn default() -> Self {
        Self::new()
    }
}

impl Sha256 {
    pub fn new() -> Self {
        Sha256 {
            h: [
                0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
                0x5be0cd19,
            ],
            buf: [0; 64],
            buflen: 0,
            len: 0,
        }
    }

    fn compress(h: &mut [u32; 8], block: &[u8]) {
        let mut w = [0u32; 64];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([
                block[i * 4],
                block[i * 4 + 1],
                block[i * 4 + 2],
                block[i * 4 + 3],
            ]);
        }
        for i in 16..64 {
            let s0 = w[i - 15].rotate_right(7) ^ w[i - 15].rotate_right(18) ^ (w[i - 15] >> 3);
            let s1 = w[i - 2].rotate_right(17) ^ w[i - 2].rotate_right(19) ^ (w[i - 2] >> 10);
            w[i] = w[i - 16]
                .wrapping_add(s0)
                .wrapping_add(w[i - 7])
                .wrapping_add(s1);
        }
        let [mut a, mut b, mut c, mut d, mut e, mut f, mut g, mut hh] = *h;
        for i in 0..64 {
            let s1 = e.rotate_right(6) ^ e.rotate_right(11) ^ e.rotate_right(25);
            let ch = (e & f) ^ ((!e) & g);
            let t1 = hh
                .wrapping_add(s1)
                .wrapping_add(ch)
                .wrapping_add(K[i])
                .wrapping_add(w[i]);
            let s0 = a.rotate_right(2) ^ a.rotate_right(13) ^ a.rotate_right(22);
            let maj = (a & b) ^ (a & c) ^ (b & c);
            let t2 = s0.wrapping_add(maj);
            hh = g;
            g = f;
            f = e;
            e = d.wrapping_add(t1);
            d = c;
            c = b;
            b = a;
            a = t1.wrapping_add(t2);
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e, f, g, hh]) {
            *x = x.wrapping_add(y);
        }
    }

    pub fn update(&mut self, mut data: &[u8]) {
        self.len = self.len.wrapping_add(data.len() as u64);
        if self.buflen > 0 {
            let n = (64 - self.buflen).min(data.len());
            self.buf[self.buflen..self.buflen + n].copy_from_slice(&data[..n]);
            self.buflen += n;
            data = &data[n..];
            if self.buflen == 64 {
                let b = self.buf;
                Self::compress(&mut self.h, &b);
                self.buflen = 0;
            }
        }
        let whole = data.len() / 64 * 64;
        if whole > 0 {
            compress_blocks(&mut self.h, &data[..whole]);
            data = &data[whole..];
        }
        if !data.is_empty() {
            self.buf[..data.len()].copy_from_slice(data);
            self.buflen = data.len();
        }
    }

    pub fn finalize(mut self) -> [u8; 32] {
        let bitlen = self.len.wrapping_mul(8);
        let padlen = if self.buflen < 56 {
            56 - self.buflen
        } else {
            120 - self.buflen
        };
        let mut pad = vec![0u8; padlen];
        pad[0] = 0x80;
        pad.extend_from_slice(&bitlen.to_be_bytes());
        self.update(&pad);
        debug_assert_eq!(self.buflen, 0);
        let mut out = [0u8; 32];
        for (i, v) in self.h.iter().enumerate() {
            out[i * 4..i * 4 + 4].copy_from_slice(&v.to_be_bytes());
        }
        out
    }

    pub fn finalize_hex(self) -> String {
        hex(&self.finalize())
    }
}

/// Every 64-byte block of `blocks`, with SHA-NI when the processor has it.
fn compress_blocks(h: &mut [u32; 8], blocks: &[u8]) {
    #[cfg(target_arch = "x86_64")]
    {
        if ni::available() {
            // SAFETY: the processor has the SHA, SSE4.1 and SSSE3 extensions
            unsafe { ni::compress(h, blocks) };
            return;
        }
    }
    for b in blocks.chunks_exact(64) {
        Sha256::compress(h, b);
    }
}

#[cfg(target_arch = "x86_64")]
mod ni {
    use super::K;
    use std::arch::x86_64::*;
    use std::sync::OnceLock;

    pub fn available() -> bool {
        static HAS: OnceLock<bool> = OnceLock::new();
        *HAS.get_or_init(|| {
            std::env::var_os("YONE_NO_SHANI").is_none()
                && is_x86_feature_detected!("sha")
                && is_x86_feature_detected!("sse4.1")
                && is_x86_feature_detected!("ssse3")
        })
    }

    #[inline(always)]
    unsafe fn schedule(v0: __m128i, v1: __m128i, v2: __m128i, v3: __m128i) -> __m128i {
        let t1 = _mm_sha256msg1_epu32(v0, v1);
        let t2 = _mm_alignr_epi8(v3, v2, 4);
        let t3 = _mm_add_epi32(t1, t2);
        _mm_sha256msg2_epu32(t3, v3)
    }

    /// Four rounds: the message words `w` with the constants K[4i..4i+4].
    #[inline(always)]
    unsafe fn rounds4(abef: &mut __m128i, cdgh: &mut __m128i, w: __m128i, i: usize) {
        let k = _mm_loadu_si128(K.as_ptr().add(4 * i) as *const __m128i);
        let t1 = _mm_add_epi32(w, k);
        *cdgh = _mm_sha256rnds2_epu32(*cdgh, *abef, t1);
        let t2 = _mm_shuffle_epi32(t1, 0x0E);
        *abef = _mm_sha256rnds2_epu32(*abef, *cdgh, t2);
    }

    #[target_feature(enable = "sha,sse2,ssse3,sse4.1")]
    pub unsafe fn compress(state: &mut [u32; 8], blocks: &[u8]) {
        let mask = _mm_set_epi64x(
            0x0c0d_0e0f_0809_0a0b_u64 as i64,
            0x0405_0607_0001_0203_u64 as i64,
        );
        let p = state.as_ptr() as *const __m128i;
        let dcba = _mm_loadu_si128(p);
        let efgh = _mm_loadu_si128(p.add(1));
        let cdab = _mm_shuffle_epi32(dcba, 0xB1);
        let efgh = _mm_shuffle_epi32(efgh, 0x1B);
        let mut abef = _mm_alignr_epi8(cdab, efgh, 8);
        let mut cdgh = _mm_blend_epi16(efgh, cdab, 0xF0);
        for block in blocks.chunks_exact(64) {
            let (abef_save, cdgh_save) = (abef, cdgh);
            let d = block.as_ptr() as *const __m128i;
            let mut w0 = _mm_shuffle_epi8(_mm_loadu_si128(d), mask);
            let mut w1 = _mm_shuffle_epi8(_mm_loadu_si128(d.add(1)), mask);
            let mut w2 = _mm_shuffle_epi8(_mm_loadu_si128(d.add(2)), mask);
            let mut w3 = _mm_shuffle_epi8(_mm_loadu_si128(d.add(3)), mask);
            rounds4(&mut abef, &mut cdgh, w0, 0);
            rounds4(&mut abef, &mut cdgh, w1, 1);
            rounds4(&mut abef, &mut cdgh, w2, 2);
            rounds4(&mut abef, &mut cdgh, w3, 3);
            let mut w4;
            w4 = schedule(w0, w1, w2, w3);
            rounds4(&mut abef, &mut cdgh, w4, 4);
            w0 = schedule(w1, w2, w3, w4);
            rounds4(&mut abef, &mut cdgh, w0, 5);
            w1 = schedule(w2, w3, w4, w0);
            rounds4(&mut abef, &mut cdgh, w1, 6);
            w2 = schedule(w3, w4, w0, w1);
            rounds4(&mut abef, &mut cdgh, w2, 7);
            w3 = schedule(w4, w0, w1, w2);
            rounds4(&mut abef, &mut cdgh, w3, 8);
            w4 = schedule(w0, w1, w2, w3);
            rounds4(&mut abef, &mut cdgh, w4, 9);
            w0 = schedule(w1, w2, w3, w4);
            rounds4(&mut abef, &mut cdgh, w0, 10);
            w1 = schedule(w2, w3, w4, w0);
            rounds4(&mut abef, &mut cdgh, w1, 11);
            w2 = schedule(w3, w4, w0, w1);
            rounds4(&mut abef, &mut cdgh, w2, 12);
            w3 = schedule(w4, w0, w1, w2);
            rounds4(&mut abef, &mut cdgh, w3, 13);
            w4 = schedule(w0, w1, w2, w3);
            rounds4(&mut abef, &mut cdgh, w4, 14);
            w0 = schedule(w1, w2, w3, w4);
            rounds4(&mut abef, &mut cdgh, w0, 15);
            abef = _mm_add_epi32(abef, abef_save);
            cdgh = _mm_add_epi32(cdgh, cdgh_save);
        }
        let feba = _mm_shuffle_epi32(abef, 0x1B);
        let dchg = _mm_shuffle_epi32(cdgh, 0xB1);
        let dcba = _mm_blend_epi16(feba, dchg, 0xF0);
        let hgef = _mm_alignr_epi8(dchg, feba, 8);
        let q = state.as_mut_ptr() as *mut __m128i;
        _mm_storeu_si128(q, dcba);
        _mm_storeu_si128(q.add(1), hgef);
    }
}

pub fn hex(bytes: &[u8]) -> String {
    const D: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(D[(b >> 4) as usize] as char);
        s.push(D[(b & 15) as usize] as char);
    }
    s
}

#[allow(dead_code)]
pub fn bytes_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize_hex()
}

pub fn file_hex(path: &Path) -> io::Result<String> {
    let mut f = File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(h.finalize_hex())
}

pub fn is_hex64(s: &str) -> bool {
    s.len() == 64
        && s.bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

/// A writer that hashes everything passing through it.
pub struct HashingWriter<W: Write> {
    pub inner: W,
    pub hasher: Sha256,
    pub written: u64,
}

impl<W: Write> HashingWriter<W> {
    pub fn new(inner: W) -> Self {
        HashingWriter {
            inner,
            hasher: Sha256::new(),
            written: 0,
        }
    }
}

impl<W: Write> Write for HashingWriter<W> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = self.inner.write(buf)?;
        self.hasher.update(&buf[..n]);
        self.written += n as u64;
        Ok(n)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.flush()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nist_vectors() {
        assert_eq!(
            bytes_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            bytes_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
        assert_eq!(
            bytes_hex(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
            "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        );
        let mut h = Sha256::new();
        let chunk = [b'a'; 1000];
        for _ in 0..1000 {
            h.update(&chunk);
        }
        assert_eq!(
            h.finalize_hex(),
            "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
        );
    }

    #[test]
    fn sha_ni_matches_portable() {
        // every length from 0 to 300 bytes, and a large buffer
        let data: Vec<u8> = (0..70_000u32)
            .map(|i| (i.wrapping_mul(2654435761) >> 13) as u8)
            .collect();
        for len in (0..300).chain([4096, 65_536, 70_000]) {
            let mut fast = [
                0x6a09e667u32,
                0xbb67ae85,
                0x3c6ef372,
                0xa54ff53a,
                0x510e527f,
                0x9b05688c,
                0x1f83d9ab,
                0x5be0cd19,
            ];
            let mut slow = fast;
            let whole = len / 64 * 64;
            compress_blocks(&mut fast, &data[..whole]);
            for b in data[..whole].chunks_exact(64) {
                Sha256::compress(&mut slow, b);
            }
            assert_eq!(fast, slow, "length {len}");
        }
    }

    #[test]
    fn split_updates_match() {
        let data: Vec<u8> = (0..1000u32).map(|i| (i * 7 % 251) as u8).collect();
        let whole = bytes_hex(&data);
        for split in [1usize, 55, 56, 63, 64, 65, 127, 500] {
            let mut h = Sha256::new();
            for c in data.chunks(split) {
                h.update(c);
            }
            assert_eq!(h.finalize_hex(), whole, "split {split}");
        }
    }
}
