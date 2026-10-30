// SPDX-License-Identifier: MIT
//! Sampler tests against fixture trees; the live /proc is only touched by the smoke test.

mod common;

use std::time::{Duration, Instant};

use common::{TempDir, copy_tree, fixture, write};
use vitals::sample::cpu::CpuSampler;
use vitals::sample::disk::DiskSampler;
use vitals::sample::hwmon::Hwmon;
use vitals::sample::net::{NetSampler, PEAK_FLOOR_BPS};
use vitals::sample::{Root, System};

fn root(t: &TempDir) -> Root {
    Root::new(t.join("proc"), t.join("sys"))
}

#[test]
fn cpu_percent_is_busy_over_total_with_iowait_idle() {
    let t = TempDir::new("cpu");
    let stat = t.join("proc/stat");
    write(
        &stat,
        "cpu  0 0 0 0 0 0 0 0\ncpu0 100 0 50 700 0 0 0 0 0 0\ncpu1 300 0 150 700 0 0 0 0 0 0\n",
    );
    write(&t.join("proc/loadavg"), "0.52 1.25 2.00 3/900 12345\n");
    let mut cpu = CpuSampler::new(&root(&t)).unwrap();
    cpu.sample().unwrap();
    assert!(!cpu.ready());
    // cpu0: busy +50 of +100; cpu1: busy +100 of +100 (iowait counts as idle on cpu0: +10 idle)
    write(
        &stat,
        "cpu  0 0 0 0 0 0 0 0\ncpu0 140 0 60 740 10 0 0 0 0 0\ncpu1 400 0 250 700 0 0 0 0 0 0\n",
    );
    cpu.sample().unwrap();
    assert!(cpu.ready());
    assert_eq!(cpu.percent(), &[50.0, 100.0]);
    assert_eq!(cpu.mean(), 75.0);
    assert_eq!(cpu.load(), [0.52, 1.25, 2.0]);
    assert_eq!(cpu.freq_ghz(), None);
}

#[test]
fn cpu_missing_thread_and_backwards_counters_read_zero() {
    let t = TempDir::new("cpu2");
    let stat = t.join("proc/stat");
    write(&stat, "cpu0 100 0 0 100 0 0 0 0\n");
    let mut cpu = CpuSampler::new(&root(&t)).unwrap();
    cpu.sample().unwrap();
    // cpu0's counters went backwards; cpu1 is new (missing from the older sample).
    write(&stat, "cpu0 50 0 0 50 0 0 0 0\ncpu1 500 0 0 500 0 0 0 0\n");
    cpu.sample().unwrap();
    assert_eq!(cpu.percent(), &[0.0, 0.0]);
    write(
        &stat,
        "cpu0 150 0 0 150 0 0 0 0\ncpu1 600 0 0 600 0 0 0 0\n",
    );
    cpu.sample().unwrap();
    assert_eq!(cpu.percent(), &[50.0, 50.0]);
}

#[test]
fn cpu_frequency_averages_the_policies() {
    let t = TempDir::new("freq");
    copy_tree(&fixture("sys"), &t.join("sys"));
    write(&t.join("proc/stat"), "cpu0 1 0 0 1 0 0 0 0\n");
    let mut cpu = CpuSampler::new(&root(&t)).unwrap();
    cpu.sample().unwrap();
    assert_eq!(cpu.freq_ghz(), Some(3.0));
}

#[test]
fn hwmon_finds_sensors_by_name() {
    let t = TempDir::new("hwmon");
    copy_tree(&fixture("sys"), &t.join("sys"));
    let mut h = Hwmon::new(&root(&t));
    h.sample().unwrap();
    assert_eq!(h.cpu_c(), Some(52.125));
    assert_eq!(h.nvme_c(), Some(41.85));
    let empty = TempDir::new("hwmon-empty");
    let mut none = Hwmon::new(&root(&empty));
    none.sample().unwrap();
    assert_eq!((none.cpu_c(), none.nvme_c()), (None, None));
}

const DISKSTATS_1: &str = "   8       0 sda 100 0 2000 50 200 0 4000 80 0 1000 0\n   8       1 sda1 90 0 1800 40 190 0 3800 70 0 900 0\n 259       0 nvme0n1 10 0 100 5 20 0 300 8 0 100 0\n   7       0 loop0 5 0 999 1 0 0 0 0 0 5 0\n 252       0 dm-0 5 0 999 1 5 0 999 1 0 5 0\n";
const DISKSTATS_2: &str = "   8       0 sda 100 0 4000 50 200 0 8000 80 0 1500 0\n   8       1 sda1 90 0 3800 40 190 0 7800 70 0 1400 0\n 259       0 nvme0n1 10 0 300 5 20 0 700 8 0 100 0\n   7       0 loop0 5 0 9999 1 0 0 0 0 0 500 0\n 252       0 dm-0 5 0 9999 1 5 0 9999 1 0 500 0\n";

#[test]
fn disk_rates_use_whole_disks_only() {
    let t = TempDir::new("disk");
    let stats = t.join("proc/diskstats");
    write(&stats, DISKSTATS_1);
    let mut disk = DiskSampler::new(&root(&t)).unwrap();
    let t0 = Instant::now();
    disk.sample(t0).unwrap();
    assert_eq!(disk.rates().read_bps, 0.0);
    write(&stats, DISKSTATS_2);
    disk.sample(t0 + Duration::from_secs(2)).unwrap();
    let r = disk.rates();
    // sda: +2000 sectors read, +4000 written; nvme0n1: +200 read, +400 written; over 2 s.
    assert_eq!(r.read_bps, (2200.0 * 512.0) / 2.0);
    assert_eq!(r.write_bps, (4400.0 * 512.0) / 2.0);
    // sda was busy 500 ms of 2000 ms.
    assert_eq!(r.busy_pct, 25.0);
    let names: Vec<_> = disk.devices().iter().map(|d| d.name.as_str()).collect();
    assert_eq!(names, ["sda", "nvme0n1"]);
}

#[test]
fn disk_counters_going_back_read_zero() {
    let t = TempDir::new("disk2");
    let stats = t.join("proc/diskstats");
    write(&stats, DISKSTATS_2);
    let mut disk = DiskSampler::new(&root(&t)).unwrap();
    let t0 = Instant::now();
    disk.sample(t0).unwrap();
    write(&stats, DISKSTATS_1);
    disk.sample(t0 + Duration::from_secs(1)).unwrap();
    assert_eq!(disk.rates().read_bps, 0.0);
    assert_eq!(disk.rates().busy_pct, 0.0);
}

#[test]
fn mount_space_is_read_with_statvfs() {
    let t = TempDir::new("mounts");
    write(&t.join("proc/diskstats"), DISKSTATS_1);
    let target = t.join("mnt");
    std::fs::create_dir_all(&target).unwrap();
    write(
        &t.join("proc/self/mounts"),
        format!(
            "/dev/sdz1 {} ext4 rw 0 0\n/dev/sdz1 /second ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n",
            target.display()
        ),
    );
    let mut disk = DiskSampler::new(&root(&t)).unwrap();
    disk.sample(Instant::now()).unwrap();
    let m = disk.mounts();
    assert_eq!(m.len(), 1);
    assert_eq!(m[0].disk, "sdz");
    assert!(m[0].total_bytes > 0 && m[0].used_bytes <= m[0].total_bytes);
    assert!(m[0].used_percent() >= 0.0);
}

const NET_1: &str = "Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n    lo: 5000 10 0 0 0 0 0 0 5000 10 0 0 0 0 0 0\nwlp2s0: 1000 10 0 0 0 0 0 0 400 10 0 0 0 0 0 0\ndocker0: 9000 10 0 0 0 0 0 0 9000 10 0 0 0 0 0 0\n eth0: 2000 10 0 0 0 0 0 0 100 10 0 0 0 0 0 0\n";
const NET_2: &str = "Inter-|   Receive                                                |  Transmit\n face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n    lo: 9999999 10 0 0 0 0 0 0 9999999 10 0 0 0 0 0 0\nwlp2s0: 3000 10 0 0 0 0 0 0 1400 10 0 0 0 0 0 0\ndocker0: 99000 10 0 0 0 0 0 0 99000 10 0 0 0 0 0 0\n eth0: 2000 10 0 0 0 0 0 0 100 10 0 0 0 0 0 0\n";

fn net_root(t: &TempDir) -> Root {
    for name in ["wlp2s0", "eth0"] {
        write(&t.join(format!("sys/class/net/{name}/device/.keep")), "");
    }
    for name in ["lo", "docker0"] {
        write(&t.join(format!("sys/class/net/{name}/.keep")), "");
    }
    root(t)
}

#[test]
fn network_sums_physical_interfaces_only() {
    let t = TempDir::new("net");
    let dev = t.join("proc/net/dev");
    write(&dev, NET_1);
    let mut net = NetSampler::new(&net_root(&t)).unwrap();
    let t0 = Instant::now();
    net.sample(t0).unwrap();
    write(&dev, NET_2);
    net.sample(t0 + Duration::from_secs(2)).unwrap();
    let total = net.total();
    assert_eq!(total.down_bps, 1000.0); // wlp2s0 +2000 over 2 s, eth0 +0
    assert_eq!(total.up_bps, 500.0);
    let names: Vec<_> = net.physical().map(|i| i.name.as_str()).collect();
    assert_eq!(names, ["wlp2s0", "eth0"]);
    assert_eq!(net.physical().next().unwrap().rate().down_bps, 1000.0);
}

fn sample_net(include: &[&str], exclude: &[&str]) -> NetSampler {
    let t = TempDir::new("netinc");
    let dev = t.join("proc/net/dev");
    write(&dev, NET_1);
    let mut net = NetSampler::new(&net_root(&t)).unwrap();
    let own = |l: &[&str]| l.iter().map(|s| (*s).to_owned()).collect::<Vec<_>>();
    net.set_overrides(&own(include), &own(exclude));
    let t0 = Instant::now();
    net.sample(t0).unwrap();
    write(&dev, NET_2);
    net.sample(t0 + Duration::from_secs(2)).unwrap();
    net
}

#[test]
fn every_interface_is_listed_and_physical_ones_are_included_by_default() {
    let net = sample_net(&[], &[]);
    let all: Vec<_> = net
        .interfaces()
        .map(|i| (i.name.as_str(), i.included()))
        .collect();
    assert_eq!(
        all,
        [
            ("lo", false),
            ("wlp2s0", true),
            ("docker0", false),
            ("eth0", true)
        ]
    );
    assert_eq!(net.included().count(), 2);
    // The sum is the included ones only: wlp2s0 +2000 down and +1000 up over 2 s.
    assert_eq!(net.total().down_bps, 1000.0);
    assert_eq!(net.total().up_bps, 500.0);
    // lo and docker0 moved a lot but do not count.
    assert!(
        net.interfaces()
            .find(|i| i.name == "lo")
            .unwrap()
            .rate()
            .down_bps
            > 1e6
    );
}

#[test]
fn include_and_exclude_override_the_default_rule() {
    let net = sample_net(&["docker0"], &["wlp2s0"]);
    let on: Vec<_> = net.included().map(|i| i.name.as_str()).collect();
    assert_eq!(on, ["docker0", "eth0"]);
    // docker0 +90000 each way over 2 s; wlp2s0 is out.
    assert_eq!(net.total().down_bps, 45_000.0);
    assert_eq!(net.total().up_bps, 45_000.0);
    // Exclude wins over include.
    let net = sample_net(&["eth0"], &["eth0"]);
    assert!(net.included().all(|i| i.name != "eth0"));
}

#[test]
fn changing_the_overrides_resums_at_once_and_restarts_the_summed_peak() {
    let mut net = sample_net(&[], &[]);
    assert_eq!(net.total().down_bps, 1000.0);
    net.set_overrides(&["lo".to_owned()], &[]);
    assert!(
        net.total().down_bps > 1e6,
        "lo now counts, no new sample needed"
    );
    net.set_overrides(&[], &[]);
    assert_eq!(net.total().down_bps, 1000.0);
    assert_eq!(
        net.peak().down_bps,
        PEAK_FLOOR_BPS,
        "the old sum's peak is forgotten"
    );
}

#[test]
fn bytes_since_start_count_included_interfaces_from_the_first_read() {
    let net = sample_net(&[], &[]);
    // wlp2s0 +2000 rx, +1000 tx; eth0 unchanged.
    assert_eq!(net.since_start(), (2000, 1000));
    let net = sample_net(&["docker0"], &[]);
    assert_eq!(net.since_start(), (2000 + 90_000, 1000 + 90_000));
}

#[test]
fn network_peak_has_a_floor_and_remembers_five_minutes() {
    let t = TempDir::new("netpeak");
    let dev = t.join("proc/net/dev");
    let line = |rx: u64| format!("wlp2s0: {rx} 1 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n");
    write(&dev, line(0));
    let mut net = NetSampler::new(&net_root(&t)).unwrap();
    let t0 = Instant::now();
    net.sample(t0).unwrap();
    assert_eq!(net.peak().down_bps, PEAK_FLOOR_BPS);
    write(&dev, line(5_000_000));
    net.sample(t0 + Duration::from_secs(1)).unwrap();
    assert_eq!(net.peak().down_bps, 5_000_000.0);
    write(&dev, line(5_000_100));
    net.sample(t0 + Duration::from_secs(100)).unwrap();
    assert_eq!(net.peak().down_bps, 5_000_000.0);
    write(&dev, line(5_000_200));
    net.sample(t0 + Duration::from_secs(302)).unwrap();
    assert_eq!(net.peak().down_bps, PEAK_FLOOR_BPS);
    assert_eq!(net.peak_of("wlp2s0").unwrap().up_bps, PEAK_FLOOR_BPS);
    assert!(net.peak_of("nope").is_none());
}

#[test]
fn captured_laptop_fixtures_give_sane_numbers() {
    let t = TempDir::new("real");
    copy_tree(&fixture("sys"), &t.join("sys"));
    copy_tree(&fixture("proc/a"), &t.join("proc"));
    let mut sys = System::new(&root(&t)).unwrap();
    let t0 = Instant::now();
    sys.sample(t0).unwrap();
    copy_tree(&fixture("proc/b"), &t.join("proc"));
    sys.sample(t0 + Duration::from_secs(2)).unwrap();
    assert!(sys.cpu.ready());
    assert_eq!(sys.cpu.percent().len(), 16);
    assert!(sys.cpu.percent().iter().all(|p| (0.0..=100.0).contains(p)));
    let m = sys.mem.mem();
    assert!(m.total > 0 && m.used() <= m.total && m.cached > 0);
    assert!(sys.disk.devices().iter().any(|d| d.name == "nvme0n1"));
    assert!(sys.disk.devices().iter().all(|d| !d.name.contains('p')));
    let phys: Vec<_> = sys.net.physical().map(|i| i.name.as_str()).collect();
    assert_eq!(phys, ["enx3c8cf8ff16ee", "wlo1"]);
    assert!(sys.disk.mounts().iter().any(|m| m.target == "/"));
}

#[test]
fn live_system_smoke() {
    // The real /proc and /sys: only checks that it opens and reads without error.
    let mut sys = System::new(&Root::system()).unwrap();
    let t0 = Instant::now();
    sys.sample(t0).unwrap();
    sys.sample(t0 + Duration::from_secs(1)).unwrap();
    assert!(sys.mem.mem().total > 0);
    assert!(!sys.cpu.percent().is_empty());
}
