//! `.yon` package files: a tar stream (first entry `.YONEINFO`) compressed
//! with zstd or xz. Decompression streams through the system tools.

use crate::error::{Context, Result};
use crate::pkginfo::PkgInfo;
use crate::tar::{Entry, Kind, TarReader};
use crate::{bail, util};
use std::fs::File;
use std::io::{self, Read};
use std::path::Path;
use std::process::{Child, ChildStdout, Command, Stdio};

pub const INFO_NAME: &str = ".YONEINFO";
/// The file list, right after the metadata: the conflict check reads it
/// instead of decompressing the whole package. Extraction compares every
/// entry with it and refuses a package whose content differs.
pub const FILES_NAME: &str = ".YONEFILES";

/// One manifest line: kind mode size uid gid user group xattrs path target,
/// separated by tabs (paths never hold control characters).
pub fn manifest_line(e: &Entry) -> String {
    let (k, target) = match &e.kind {
        Kind::File => ('f', ""),
        Kind::Dir => ('d', ""),
        Kind::Symlink(t) => ('l', t.as_str()),
        Kind::Hardlink(t) => ('h', t.as_str()),
    };
    let x = if e.xattrs.is_empty() {
        "-".to_string()
    } else {
        e.xattrs
            .iter()
            .map(|(n, v)| format!("{n}={}", crate::sha256::hex(v)))
            .collect::<Vec<_>>()
            .join(",")
    };
    format!(
        "{k}\t{:o}\t{}\t{}\t{}\t{}\t{}\t{x}\t{}\t{target}\n",
        e.mode, e.size, e.owner.uid, e.owner.gid, e.owner.user, e.owner.group, e.path
    )
}

fn parse_manifest(text: &str, path: &Path) -> Result<Vec<Entry>> {
    let bad = || crate::err!("{}: damaged {}", path.display(), FILES_NAME);
    let mut out = vec![];
    for line in text.lines() {
        let f: Vec<&str> = line.split('\t').collect();
        if f.len() != 10 {
            return Err(bad());
        }
        let target = f[9].to_string();
        let kind = match f[0] {
            "f" => Kind::File,
            "d" => Kind::Dir,
            "l" => Kind::Symlink(target),
            "h" => Kind::Hardlink(target),
            _ => return Err(bad()),
        };
        let mut xattrs = vec![];
        if f[7] != "-" {
            for kv in f[7].split(',') {
                let (n, v) = kv.split_once('=').ok_or_else(bad)?;
                let bytes = (0..v.len())
                    .step_by(2)
                    .map(|i| u8::from_str_radix(v.get(i..i + 2).unwrap_or("zz"), 16))
                    .collect::<std::result::Result<Vec<u8>, _>>()
                    .map_err(|_| bad())?;
                xattrs.push((n.to_string(), bytes));
            }
        }
        out.push(Entry {
            path: f[8].to_string(),
            mode: u32::from_str_radix(f[1], 8).map_err(|_| bad())?,
            size: f[2].parse().map_err(|_| bad())?,
            kind,
            xattrs,
            owner: crate::tar::Owner {
                uid: f[3].parse().map_err(|_| bad())?,
                gid: f[4].parse().map_err(|_| bad())?,
                user: f[5].to_string(),
                group: f[6].to_string(),
            },
        });
    }
    Ok(out)
}

pub struct Decompressor {
    child: Child,
    // A tar archive is read in 512-byte headers and small files: a large
    // buffer (and pipe) turns hundreds of thousands of reads into a few
    // thousand.
    out: io::BufReader<ChildStdout>,
    tool: &'static str,
}

impl Read for Decompressor {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.out.read(buf)
    }
}

impl Decompressor {
    pub fn open(path: &Path) -> Result<Decompressor> {
        let mut magic = [0u8; 6];
        let n = File::open(path)
            .ctx(|| format!("open {}", path.display()))?
            .read(&mut magic)?;
        let tool = if n >= 6 && magic == [0xFD, b'7', b'z', b'X', b'Z', 0] {
            "xz"
        } else if n >= 4 && magic[..4] == [0x28, 0xB5, 0x2F, 0xFD] {
            "zstd"
        } else if n >= 2 && magic[..2] == [0x1F, 0x8B] {
            "gzip"
        } else {
            bail!("{}: unknown compression format", path.display());
        };
        let mut child = Command::new(tool)
            .arg("-dc")
            .arg(path)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| crate::err!("cannot run {tool}: {e}"))?;
        let out = child.stdout.take().unwrap();
        grow_pipe(&out);
        Ok(Decompressor {
            child,
            out: io::BufReader::with_capacity(1 << 20, out),
            tool,
        })
    }

    /// Wait for the decompressor and make sure it exited cleanly.
    pub fn finish(mut self) -> Result<()> {
        let st = self.child.wait()?;
        if !st.success() {
            bail!("{} reported a corrupt stream", self.tool);
        }
        Ok(())
    }
}

/// Ask for a 1 MiB pipe (the default is 64 KiB); harmless when refused.
fn grow_pipe(p: &ChildStdout) {
    use std::os::unix::io::AsRawFd;
    extern "C" {
        fn fcntl(fd: i32, cmd: i32, ...) -> i32;
    }
    const F_SETPIPE_SZ: i32 = 1031;
    // SAFETY: fcntl on a file descriptor this process owns
    unsafe {
        fcntl(p.as_raw_fd(), F_SETPIPE_SZ, 1 << 20);
    }
}

impl Drop for Decompressor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

pub fn compress(src: &Path, dst: &Path, algo: &str) -> Result<()> {
    let (tool, args): (&str, Vec<&str>) = match algo {
        "zstd" => ("zstd", vec!["-q", "-19", "-T0", "-c"]),
        "xz" => ("xz", vec!["-6", "-T0", "-c"]),
        other => bail!("unsupported compression '{other}' (use zstd or xz)"),
    };
    let input = File::open(src)?;
    let output = File::create(dst).ctx(|| format!("create {}", dst.display()))?;
    let st = Command::new(tool)
        .args(&args)
        .stdin(input)
        .stdout(output)
        .status()
        .map_err(|e| crate::err!("cannot run {tool}: {e} (install it or set compression = xz)"))?;
    if !st.success() {
        bail!("{tool} failed");
    }
    Ok(())
}

pub fn open_tar(path: &Path) -> Result<TarReader<Decompressor>> {
    Ok(TarReader::new(Decompressor::open(path)?))
}

fn read_info_entry<R: Read>(tr: &mut TarReader<R>, path: &Path) -> Result<PkgInfo> {
    let e = tr.next_entry().ctx(|| format!("read {}", path.display()))?;
    match e {
        Some(e) if e.path == INFO_NAME && e.kind == Kind::File && e.size < 1 << 20 => {
            let mut v = vec![];
            tr.copy_data(&mut v)?;
            PkgInfo::parse(&String::from_utf8_lossy(&v))
                .ctx(|| format!("{}: {}", path.display(), INFO_NAME))
        }
        _ => bail!(
            "{}: not a Yone package (missing {})",
            path.display(),
            INFO_NAME
        ),
    }
}

pub fn read_info(path: &Path) -> Result<PkgInfo> {
    let mut tr = open_tar(path)?;
    read_info_entry(&mut tr, path)
}

pub struct Listing {
    pub info: PkgInfo,
    pub entries: Vec<Entry>,
}

/// Read metadata and the full, validated file list.
pub fn list(path: &Path) -> Result<Listing> {
    let mut tr = open_tar(path)?;
    let info = read_info_entry(&mut tr, path)?;
    let mut entries = vec![];
    let first = tr.next_entry().ctx(|| format!("read {}", path.display()))?;
    let mut pending = match first {
        Some(e) if e.path == FILES_NAME && e.kind == Kind::File && e.size < 1 << 28 => {
            // the manifest: no need to decompress the rest
            let mut v = vec![];
            tr.copy_data(&mut v)?;
            for mut e in parse_manifest(&String::from_utf8_lossy(&v), path)? {
                e.path = util::safe_rel_path(&e.path).ctx(|| path.display().to_string())?;
                if e.path.starts_with(".YONE") {
                    bail!("{}: reserved path {}", path.display(), e.path);
                }
                if let Kind::Hardlink(t) = &e.kind {
                    e.kind = Kind::Hardlink(util::safe_rel_path(t)?);
                }
                entries.push(e);
            }
            return Ok(Listing { info, entries });
        }
        other => other,
    };
    while let Some(mut e) = match pending.take() {
        Some(e) => Some(e),
        None => tr.next_entry().ctx(|| format!("read {}", path.display()))?,
    } {
        e.path = util::safe_rel_path(&e.path).ctx(|| path.display().to_string())?;
        if e.path.starts_with(".YONE") {
            bail!("{}: reserved path {}", path.display(), e.path);
        }
        if let Kind::Hardlink(t) = &e.kind {
            e.kind = Kind::Hardlink(util::safe_rel_path(t)?);
        }
        entries.push(e);
    }
    tr.into_inner()
        .finish()
        .ctx(|| path.display().to_string())?;
    Ok(Listing { info, entries })
}
