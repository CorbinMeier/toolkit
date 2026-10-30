// SPDX-License-Identifier: MIT
//! Temperatures from hwmon: the CPU's `k10temp` (Tctl) and the NVMe drive, found once by `name`.

use std::io;

use super::{KeptFile, Root, parse_u64};

/// Temperature sensors, each optional: a machine without `k10temp` or an NVMe drive just has `None`.
pub struct Hwmon {
    cpu: Option<KeptFile>,
    nvme: Option<KeptFile>,
    cpu_c: Option<f32>,
    nvme_c: Option<f32>,
}

impl Hwmon {
    /// Scans `<sys>/class/hwmon/hwmon*/name` once and opens `temp1_input` of the matches.
    pub fn new(root: &Root) -> Self {
        let mut cpu = None;
        let mut nvme = None;
        if let Ok(dir) = std::fs::read_dir(root.sys.join("class/hwmon")) {
            let mut entries: Vec<_> = dir.flatten().map(|e| e.path()).collect();
            entries.sort();
            for path in entries {
                let Ok(name) = std::fs::read_to_string(path.join("name")) else {
                    continue;
                };
                let slot = match name.trim() {
                    "k10temp" => &mut cpu,
                    "nvme" => &mut nvme,
                    _ => continue,
                };
                if slot.is_none() {
                    *slot = KeptFile::open(&path.join("temp1_input"), 64).ok();
                }
            }
        }
        Hwmon {
            cpu,
            nvme,
            cpu_c: None,
            nvme_c: None,
        }
    }

    pub fn sample(&mut self) -> io::Result<()> {
        // Readings are millidegrees Celsius; a failed read keeps the last value.
        let read = |file: &mut Option<KeptFile>, last: &mut Option<f32>| {
            if let Some(Ok(buf)) = file.as_mut().map(|f| f.read()) {
                *last = Some(parse_u64(buf) as f32 / 1000.0);
            }
        };
        read(&mut self.cpu, &mut self.cpu_c);
        read(&mut self.nvme, &mut self.nvme_c);
        Ok(())
    }

    /// CPU package temperature (`k10temp` Tctl) in °C.
    pub fn cpu_c(&self) -> Option<f32> {
        self.cpu_c
    }

    /// NVMe composite temperature in °C.
    pub fn nvme_c(&self) -> Option<f32> {
        self.nvme_c
    }
}
