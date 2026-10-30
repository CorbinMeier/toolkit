// SPDX-License-Identifier: MIT
//! vitals: a lightweight btop-style terminal dashboard. Flags, terminal setup and restore, the event loop.

use std::io::{BufWriter, IsTerminal, Write};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use ratatui::buffer::Buffer;
use ratatui::crossterm::Command;
use ratatui::crossterm::event::{self, Event, KeyEventKind};
use ratatui::crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::crossterm::{cursor, execute};
use ratatui::layout::Rect;

use vitals::app::App;
use vitals::claude::ClaudeSource;
use vitals::config::{self, Config, LayoutMode};
use vitals::frame;
use vitals::readout;
use vitals::sample::{Root, System};
use vitals::theme::{self, Depth, ThemeBook, ThemeInputs};
use vitals::tokens::indexer::now_ms;
use vitals::tokens::{Indexer, IndexerConfig};
use vitals::ui;

const HELP: &str = "vitals: a lightweight terminal dashboard for CPU, memory, disk, network and Claude usage

USAGE: vitals [OPTIONS]

OPTIONS:
    --interval <ms>     refresh interval in milliseconds (250, 500, 1000, 2000 or 5000; default 1000)
    --theme <name>      default, ramp, mono, or a file in ~/.config/vitals/themes/
    --no-mouse          start with mouse capture off
    --layout <mode>     auto, rows or column
    --once              print one rendered frame to stdout (no alternate screen), then exit
    --size <WxH>        with --once: the frame size (default: the terminal, then COLUMNS x LINES, then 80x24)
    --no-tokens         with --once, --all or --serve: skip the Claude token index
    --serve [ADDR:PORT] headless web dashboard (default 0.0.0.0:4318, config [web]); prints the URLs and the key

SECTION READOUTS (print and exit, no TUI; exit code 0 ok, 1 a section failed, 2 bad flags):
    --cpu               CPU: busy %, per thread, load, frequency, temperature
    --mem, --memory     memory: used, cache, available, swap
    --disk              disk: mounts, per-disk busy % and rates, NVMe temperature
    --net, --network    network: rates per interface and summed over the included ones, 5-minute peak
    --tokens            Claude token usage: today, last hour, rate, by model family
    --claude            Claude usage: session, week, per-model weeks (feed, then usage server)
    --all, --dump       every section above
    --json              print one JSON object (schema 2) instead of text
    --watch             keep printing every --interval (one JSON object per line with --json)
    --no-wait           with --tokens: do not wait for the first index backfill to finish
    --version           print the version
    --help              print this help

Keys: ? inside the program lists every key and mouse action.
";

#[derive(Default)]
struct Args {
    once: bool,
    readout: readout::Request,
    interval: Option<u64>,
    theme: Option<String>,
    no_mouse: bool,
    layout: Option<LayoutMode>,
    tokens_off: bool,
    size: Option<(u16, u16)>,
    /// `--serve`: the web dashboard instead of the TUI; the inner option is the listen address.
    serve: Option<Option<String>>,
    /// Benchmark support: quit after this many seconds and write the figures to `stats`.
    exit_after: Option<Duration>,
    stats: Option<PathBuf>,
}

fn parse_args() -> Result<Option<Args>, String> {
    let mut a = Args::default();
    let mut it = std::env::args().skip(1).peekable();
    while let Some(arg) = it.next() {
        let mut value = |name: &str| it.next().ok_or(format!("{name} needs a value"));
        match arg.as_str() {
            "--once" => a.once = true,
            "--count" => {
                let v = value("--count")?;
                a.readout.count = Some(
                    v.parse()
                        .ok()
                        .filter(|n| *n > 0)
                        .ok_or(format!("bad --count value: {v}"))?,
                );
            }
            flag if a.readout.flag(flag) => {}
            "--no-tokens" => a.tokens_off = true,
            "--serve" => {
                let addr = it.next_if(|v| !v.starts_with('-'));
                a.serve = Some(addr);
            }
            "--no-mouse" => a.no_mouse = true,
            "--interval" => {
                let v = value("--interval")?;
                a.interval = Some(
                    v.parse()
                        .map_err(|_| format!("bad --interval value: {v}"))?,
                );
            }
            "--theme" => a.theme = Some(value("--theme")?),
            "--layout" => {
                let v = value("--layout")?;
                a.layout = Some(
                    LayoutMode::parse(&v)
                        .ok_or(format!("bad --layout value: {v} (auto, rows or column)"))?,
                );
            }
            "--size" => {
                let v = value("--size")?;
                let (w, h) = v.split_once('x').ok_or(format!("bad --size value: {v}"))?;
                let n = |s: &str| {
                    s.parse::<u16>()
                        .map_err(|_| format!("bad --size value: {v}"))
                };
                a.size = Some((n(w)?, n(h)?));
            }
            "--exit-after" => {
                let v = value("--exit-after")?;
                a.exit_after = Some(Duration::from_secs_f64(
                    v.parse()
                        .map_err(|_| format!("bad --exit-after value: {v}"))?,
                ));
            }
            "--stats" => a.stats = Some(value("--stats")?.into()),
            "--version" => {
                println!("vitals {}", env!("CARGO_PKG_VERSION"));
                return Ok(None);
            }
            "--help" | "-h" => {
                print!("{HELP}");
                return Ok(None);
            }
            other => return Err(format!("unknown option: {other}\n\n{HELP}")),
        }
    }
    Ok(Some(a))
}

fn env_get(k: &str) -> Option<String> {
    std::env::var(k).ok()
}

/// What `main` needs to start: the config (with the command line applied) and the themes.
struct Setup {
    cfg: Config,
    /// The config as read from disk, to save the flags' overrides without persisting them.
    loaded: Config,
    path: Option<PathBuf>,
    themes: ThemeBook,
    warn: Option<String>,
}

fn setup(args: &Args) -> Setup {
    let dir = config::config_dir(&env_get);
    let path = dir.as_ref().map(|d| d.join("config.toml"));
    let (loaded, warn) = path
        .as_deref()
        .map_or((Config::default(), None), config::load);
    let mut cfg = loaded.clone();
    if let Some(ms) = args.interval {
        cfg.interval_ms = ms;
    }
    if let Some(t) = &args.theme {
        cfg.theme = t.clone();
    }
    if args.no_mouse {
        cfg.mouse = false;
    }
    if let Some(l) = args.layout {
        cfg.layout = l;
    }
    cfg.sanitize();
    let depth = theme::detect_depth(&env_get);
    let config_home = dir.as_ref().and_then(|d| d.parent().map(PathBuf::from));
    let inputs = ThemeInputs {
        accent: cfg.accent.as_deref().and_then(theme::parse_hex),
        cosmic_accent: config_home
            .as_deref()
            .and_then(ThemeBook::read_cosmic_accent),
    };
    let custom = dir
        .as_ref()
        .map(|d| ThemeBook::read_custom(&d.join("themes")))
        .unwrap_or_default();
    Setup {
        cfg,
        loaded,
        path,
        themes: ThemeBook::new(depth, inputs, custom),
        warn,
    }
}

/// The config to save: what the UI changed, with the command-line overrides left out of it.
fn for_save(cfg: &Config, loaded: &Config, args: &Args, started: &Config) -> Config {
    let mut out = cfg.clone();
    if args.interval.is_some() && cfg.interval_ms == started.interval_ms {
        out.interval_ms = loaded.interval_ms;
    }
    if args.theme.is_some() && cfg.theme == started.theme {
        out.theme = loaded.theme.clone();
    }
    if args.no_mouse && cfg.mouse == started.mouse {
        out.mouse = loaded.mouse;
    }
    if args.layout.is_some() {
        out.layout = loaded.layout;
    }
    out
}

/// Mouse reporting for clicks, releases and the wheel only (SGR coordinates). crossterm's own
/// `EnableMouseCapture` also asks for motion events, which would wake the program on every mouse move
/// (spec §6), so these two commands replace it.
struct EnableMouseCapture;
struct DisableMouseCapture;

impl Command for EnableMouseCapture {
    fn write_ansi(&self, f: &mut impl std::fmt::Write) -> std::fmt::Result {
        f.write_str("\x1b[?1000h\x1b[?1006h")
    }
}

impl Command for DisableMouseCapture {
    fn write_ansi(&self, f: &mut impl std::fmt::Write) -> std::fmt::Result {
        f.write_str("\x1b[?1006l\x1b[?1000l")
    }
}

static SIGNALLED: AtomicBool = AtomicBool::new(false);

extern "C" fn on_signal(_: libc::c_int) {
    SIGNALLED.store(true, Ordering::Relaxed);
}

/// SIGINT, SIGTERM and SIGHUP end the loop so the terminal is restored. (In raw mode Ctrl+C is a key.)
fn install_signal_handlers() {
    for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP] {
        // SAFETY: the handler only stores to an atomic, which is async-signal-safe.
        unsafe {
            let mut sa: libc::sigaction = std::mem::zeroed();
            sa.sa_sigaction = on_signal as extern "C" fn(libc::c_int) as usize;
            libc::sigemptyset(&mut sa.sa_mask);
            libc::sigaction(sig, &sa, std::ptr::null_mut());
        }
    }
}

fn restore_terminal() {
    let mut out = std::io::stdout();
    let _ = execute!(out, DisableMouseCapture, LeaveAlternateScreen, cursor::Show);
    let _ = disable_raw_mode();
}

/// Restores the terminal before the panic message prints (release builds abort after this hook).
fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        restore_terminal();
        previous(info);
    }));
}

fn terminal_size() -> Option<(u16, u16)> {
    // SAFETY: TIOCGWINSZ fills a plain `winsize` struct.
    let ws = unsafe {
        let mut ws: libc::winsize = std::mem::zeroed();
        (libc::ioctl(1, libc::TIOCGWINSZ, &mut ws) == 0).then_some(ws)?
    };
    (ws.ws_col > 0 && ws.ws_row > 0).then_some((ws.ws_col, ws.ws_row))
}

fn main() -> ExitCode {
    let started = Instant::now();
    let args = match parse_args() {
        Ok(Some(a)) => a,
        Ok(None) => return ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    if let Some(code) = readout::dispatch(&args.readout, args.interval, args.tokens_off, args.once)
    {
        return ExitCode::from(code);
    }
    if let Some(listen) = &args.serve {
        if args.once || args.readout.requested() {
            eprintln!("vitals: --serve cannot be combined with --once or section flags");
            return ExitCode::from(2);
        }
        let serve = vitals::web::ServeArgs {
            listen: listen.clone(),
            interval_ms: args.interval,
            theme: args.theme.clone(),
            tokens_off: args.tokens_off,
        };
        return match vitals::web::serve_main(&serve) {
            Ok(()) => ExitCode::SUCCESS,
            Err(e) => {
                eprintln!("vitals: {e}");
                ExitCode::FAILURE
            }
        };
    }
    let result = if args.once {
        once(&args)
    } else {
        run(&args, started)
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("vitals: {e}");
            ExitCode::FAILURE
        }
    }
}

fn new_app(setup: Setup, now: Instant) -> Result<App, String> {
    let mut sys = System::new(&Root::system()).map_err(|e| format!("cannot open /proc: {e}"))?;
    let _ = sys.sample(now);
    let mut app = App::new(setup.cfg, setup.themes, sys, now_ms());
    if setup.warn.is_some() {
        app.ui.warn = setup.warn;
    }
    app.tick(now, now_ms());
    Ok(app)
}

/// `--once`: sample twice one interval apart, wait for the token backfill, print one frame.
fn once(args: &Args) -> Result<(), String> {
    let setup = setup(args);
    let (w, h) = args
        .size
        .or_else(|| {
            std::io::stdout()
                .is_terminal()
                .then(terminal_size)
                .flatten()
        })
        .or_else(|| {
            let n = |k| env_get(k).and_then(|v| v.parse::<u16>().ok());
            Some((n("COLUMNS")?, n("LINES")?))
        })
        .unwrap_or((80, 24));
    let depth = setup.themes.depth;
    let interval = Duration::from_millis(setup.cfg.interval_ms);
    let usage_url = setup.cfg.usage_url().to_owned();
    let t0 = Instant::now();
    let mut app = new_app(setup, t0)?;
    let indexer = (!args.tokens_off)
        .then(|| Indexer::spawn(IndexerConfig::from_env()))
        .transpose()
        .map_err(|e| format!("cannot start the token indexer: {e}"))?;
    let mut source = ClaudeSource::new(claude_usage_core::feed::feed_dir(), Some(usage_url));
    std::thread::sleep(interval);
    if let Some(ix) = &indexer {
        readout::wait_for_backfill(ix, &mut app.data.tokens);
    }
    // Give the usage server thread a moment: its first answer arrives within its 500 ms timeouts.
    std::thread::sleep(Duration::from_millis(600));
    let now = Instant::now();
    source.poll(now, &mut app.data.claude);
    app.tick(now, now_ms());
    let area = Rect::new(0, 0, w, h);
    let mut buf = Buffer::empty(area);
    ui::render(&mut app, area, &mut buf);
    let text = if depth == Depth::Mono || !std::io::stdout().is_terminal() {
        frame::to_plain(&buf)
    } else {
        frame::to_ansi(&buf)
    };
    print!("{text}");
    let _ = std::io::stdout().flush();
    if let Some(ix) = indexer {
        ix.shutdown();
    }
    source.shutdown();
    Ok(())
}

/// utime + stime of this process in clock ticks, from `/proc/self/stat`.
fn cpu_ticks() -> u64 {
    let Ok(stat) = std::fs::read_to_string("/proc/self/stat") else {
        return 0;
    };
    // Fields after the parenthesised name; utime and stime are the 14th and 15th overall.
    let rest = stat.rsplit_once(')').map_or("", |x| x.1);
    let f: Vec<&str> = rest.split_whitespace().collect();
    let n = |i: usize| f.get(i).and_then(|v| v.parse::<u64>().ok()).unwrap_or(0);
    n(11) + n(12)
}

fn status_kb(key: &str) -> u64 {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with(key))
                .and_then(|l| l.split_whitespace().nth(1)?.parse().ok())
        })
        .unwrap_or(0)
}

fn run(args: &Args, started: Instant) -> Result<(), String> {
    let setup = setup(args);
    let (loaded, path) = (setup.loaded.clone(), setup.path.clone());
    let at_start = setup.cfg.clone();
    let usage_url = setup.cfg.usage_url().to_owned();
    let mut app = new_app(setup, Instant::now())?;
    let indexer = (!args.tokens_off)
        .then(|| Indexer::spawn(IndexerConfig::from_env()))
        .transpose()
        .map_err(|e| format!("cannot start the token indexer: {e}"))?;
    let mut source = ClaudeSource::new(claude_usage_core::feed::feed_dir(), Some(usage_url));

    install_panic_hook();
    install_signal_handlers();
    enable_raw_mode().map_err(|e| format!("cannot enter raw mode: {e}"))?;
    let mut out = BufWriter::with_capacity(256 * 1024, std::io::stdout());
    execute!(out, EnterAlternateScreen, cursor::Hide).map_err(|e| e.to_string())?;
    if app.cfg.mouse {
        execute!(out, EnableMouseCapture).map_err(|e| e.to_string())?;
    }
    let mut terminal = Terminal::new(CrosstermBackend::new(out)).map_err(|e| e.to_string())?;

    // Changes made in the UI are saved at once, without the command-line overrides.
    let save_now = |cfg: &Config| {
        if let Some(p) = &path {
            let save = for_save(cfg, &loaded, args, &at_start);
            if let Err(e) = config::save_keeping_web(p, &save) {
                eprintln!("vitals: cannot save {}: {e}", p.display());
            }
        }
    };
    let outcome = event_loop(
        &mut terminal,
        &mut app,
        indexer.as_ref(),
        &mut source,
        args,
        started,
        &save_now,
    );

    // Restore first, so any error message lands on a normal screen.
    restore_terminal();
    drop(terminal);
    save_now(&app.cfg);
    if let Some(ix) = indexer {
        ix.shutdown();
    }
    source.shutdown();
    outcome
}

fn event_loop(
    terminal: &mut Terminal<CrosstermBackend<BufWriter<std::io::Stdout>>>,
    app: &mut App,
    indexer: Option<&Indexer>,
    source: &mut ClaudeSource,
    args: &Args,
    started: Instant,
    save: &dyn Fn(&Config),
) -> Result<(), String> {
    let io = |e: std::io::Error| e.to_string();
    let mut last_tick = Instant::now();
    let mut dirty = true;
    let mut first_frame: Option<Duration> = None;
    let mut cpu_at_5s: Option<u64> = None;
    loop {
        if app.quit || SIGNALLED.load(Ordering::Relaxed) {
            break;
        }
        if args.exit_after.is_some_and(|d| started.elapsed() >= d) {
            break;
        }
        #[cfg(debug_assertions)]
        if first_frame.is_some() && std::env::var_os("VITALS_TEST_PANIC").is_some() {
            panic!("VITALS_TEST_PANIC: checking that the terminal is restored");
        }
        if dirty {
            terminal
                .draw(|f| {
                    let area = f.area();
                    ui::render(app, area, f.buffer_mut());
                })
                .map_err(io)?;
            dirty = false;
            first_frame.get_or_insert_with(|| started.elapsed());
        }
        if cpu_at_5s.is_none() && started.elapsed() >= Duration::from_secs(5) {
            cpu_at_5s = Some(cpu_ticks());
        }
        // Wake at least twice a second so a signal ends the loop promptly; drawing waits for a tick or input.
        let next_tick = last_tick + app.interval();
        let timeout = next_tick
            .saturating_duration_since(Instant::now())
            .min(Duration::from_millis(500));
        if event::poll(timeout).map_err(io)? {
            loop {
                match event::read().map_err(io)? {
                    Event::Key(k) if k.kind != KeyEventKind::Release => app.handle_key(k),
                    Event::Mouse(m) => app.handle_mouse(m),
                    _ => {}
                }
                dirty = true;
                if !event::poll(Duration::ZERO).map_err(io)? {
                    break;
                }
            }
            if std::mem::take(&mut app.cfg_changed) {
                save(&app.cfg);
            }
            if std::mem::take(&mut app.mouse_changed) {
                let w = terminal.backend_mut();
                if app.cfg.mouse {
                    execute!(w, EnableMouseCapture).map_err(io)?;
                } else {
                    execute!(w, DisableMouseCapture).map_err(io)?;
                }
            }
        }
        let now = Instant::now();
        if now >= last_tick + app.interval() {
            last_tick = now;
            if let Some(ix) = indexer {
                ix.drain_into(&mut app.data.tokens);
            }
            source.poll(now, &mut app.data.claude);
            app.tick(now, now_ms());
            dirty = true;
        }
    }
    if let Some(path) = &args.stats {
        let total = cpu_ticks();
        let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) }.max(1);
        let text = format!(
            "first_frame_ms={:.1}\nclk_tck={hz}\ncpu_ticks_total={total}\ncpu_ticks_after_5s={}\nelapsed_s={:.1}\nrss_kb={}\nhwm_kb={}\n",
            first_frame.unwrap_or_default().as_secs_f64() * 1000.0,
            cpu_at_5s.unwrap_or(total),
            started.elapsed().as_secs_f64(),
            status_kb("VmRSS:"),
            status_kb("VmHWM:"),
        );
        std::fs::write(path, text).map_err(io)?;
    }
    Ok(())
}
