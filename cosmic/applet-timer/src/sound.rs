// SPDX-License-Identifier: MIT

use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

const OGA: &str = "/usr/share/sounds/freedesktop/stereo/alarm-clock-elapsed.oga";

/// Backends in fallback order: the user's sound theme first, then raw file players.
fn backends() -> [(&'static str, Vec<&'static str>); 3] {
    [
        ("canberra-gtk-play", vec!["-i", "alarm-clock-elapsed"]),
        ("pw-play", vec![OGA]),
        ("paplay", vec![OGA]),
    ]
}

/// Loops the alarm sound until dropped or stopped. Runs on its own thread,
/// never on the UI thread; `stop` kills the current child immediately.
pub struct Ringer {
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
}

impl Ringer {
    pub fn start() -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let child: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
        let (s, c) = (stop.clone(), child.clone());
        thread::spawn(move || run(&s, &c));
        Ringer { stop, child }
    }

    pub fn stop(&self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(mut ch) = self.child.lock().unwrap().take() {
            let _ = ch.kill();
            let _ = ch.wait();
        }
    }
}

impl Drop for Ringer {
    fn drop(&mut self) {
        self.stop();
    }
}

fn run(stop: &AtomicBool, slot: &Mutex<Option<Child>>) {
    let backends = backends();
    let mut cur = 0;
    while cur < backends.len() && !stop.load(Ordering::SeqCst) {
        let (cmd, args) = &backends[cur];
        let started = Instant::now();
        {
            let mut guard = slot.lock().unwrap();
            if stop.load(Ordering::SeqCst) {
                return;
            }
            match Command::new(cmd)
                .args(args)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
            {
                Ok(ch) => *guard = Some(ch),
                Err(_) => {
                    cur += 1;
                    continue;
                }
            }
        }
        // Poll so `stop()` can take and kill the child at any time.
        let ok = loop {
            thread::sleep(Duration::from_millis(50));
            let mut guard = slot.lock().unwrap();
            match guard.as_mut() {
                None => return, // killed by stop()
                Some(ch) => match ch.try_wait() {
                    Ok(Some(status)) => {
                        *guard = None;
                        break status.success();
                    }
                    Ok(None) => {}
                    Err(_) => {
                        *guard = None;
                        break false;
                    }
                },
            }
        };
        if !ok {
            cur += 1; // backend failed: fall back to the next one
        } else if started.elapsed() < Duration::from_millis(200) {
            thread::sleep(Duration::from_millis(200)); // never spin
        }
    }
}
