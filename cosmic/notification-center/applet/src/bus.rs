// SPDX-License-Identifier: MIT

//! Client for the daemon's control interface `net.corbinmeier.Notifications1`.
//!
//! Live updates come from three broadcast signals (properties changed, notification added, and the
//! bus-name owner changing); each one triggers a single refetch of the whole [`Status`]. Nothing
//! polls. The match rules carry no sender, so they survive the daemon restarting.

use std::pin::Pin;
use std::time::Duration;

use futures_util::stream::{self, Stream, StreamExt};
use zbus::{Connection, MatchRule, MessageStream, message::Type, proxy, proxy::CacheProperties};

use crate::model::{POPUP_ROWS, Status, is_missing_method, parse_history};

pub const BUS_NAME: &str = "net.corbinmeier.Notifications1";
pub const OBJECT_PATH: &str = "/net/corbinmeier/Notifications1";

/// Overrides the session bus (tests and dev runs against a private bus).
pub const ADDRESS_ENV: &str = "NOTIFYD_BUS_ADDRESS";

#[proxy(
    interface = "net.corbinmeier.Notifications1",
    default_service = "net.corbinmeier.Notifications1",
    default_path = "/net/corbinmeier/Notifications1"
)]
trait Notifications {
    fn toggle_center(&self) -> zbus::Result<()>;
    fn show_center(&self) -> zbus::Result<()>;
    fn toggle_shade(&self) -> zbus::Result<()>;
    fn show_shade(&self) -> zbus::Result<()>;
    fn show_notification(&self, id: u32) -> zbus::Result<()>;
    fn mark_seen(&self) -> zbus::Result<()>;
    fn get_history(&self, query: &str, offset: u32, limit: u32) -> zbus::Result<String>;

    #[zbus(property)]
    fn unseen_count(&self) -> zbus::Result<u32>;
    #[zbus(property)]
    fn unread_count(&self) -> zbus::Result<u32>;
    #[zbus(property)]
    fn do_not_disturb(&self) -> zbus::Result<bool>;
    #[zbus(property)]
    fn set_do_not_disturb(&self, value: bool) -> zbus::Result<()>;
}

async fn proxy(conn: &Connection) -> zbus::Result<NotificationsProxy<'static>> {
    NotificationsProxy::builder(conn)
        .cache_properties(CacheProperties::No)
        .build()
        .await
}

/// The session bus, or the address in [`ADDRESS_ENV`] when set.
pub async fn connect() -> zbus::Result<Connection> {
    match std::env::var(ADDRESS_ENV) {
        Ok(addr) if !addr.is_empty() => connect_to(&addr).await,
        _ => Connection::session().await,
    }
}

pub async fn connect_to(address: &str) -> zbus::Result<Connection> {
    zbus::connection::Builder::address(address)?.build().await
}

pub async fn toggle_center(conn: &Connection) -> zbus::Result<()> {
    proxy(conn).await?.toggle_center().await
}

pub async fn show_center(conn: &Connection) -> zbus::Result<()> {
    proxy(conn).await?.show_center().await
}

pub async fn toggle_shade(conn: &Connection) -> zbus::Result<()> {
    proxy(conn).await?.toggle_shade().await
}

/// Not bound to a click yet (the bell toggles); the private-bus test exercises it.
#[cfg(test)]
pub async fn show_shade(conn: &Connection) -> zbus::Result<()> {
    proxy(conn).await?.show_shade().await
}

/// The bell's left click: `ToggleShade`, or `ToggleCenter` when the daemon is older and has no
/// such method. Any other failure is returned as is.
pub async fn toggle_shade_or_center(conn: &Connection) -> zbus::Result<()> {
    match toggle_shade(conn).await {
        Err(zbus::Error::MethodError(name, _, _)) if is_missing_method(name.as_str()) => {
            toggle_center(conn).await
        }
        other => other,
    }
}

/// Move the daemon's "seen" watermark to now (the new-count drops to 0).
pub async fn mark_seen(conn: &Connection) -> zbus::Result<()> {
    proxy(conn).await?.mark_seen().await
}

pub async fn show_notification(conn: &Connection, id: u32) -> zbus::Result<()> {
    proxy(conn).await?.show_notification(id).await
}

pub async fn set_dnd(conn: &Connection, on: bool) -> zbus::Result<()> {
    proxy(conn).await?.set_do_not_disturb(on).await
}

async fn has_owner(conn: &Connection) -> bool {
    let Ok(dbus) = zbus::fdo::DBusProxy::new(conn).await else {
        return false;
    };
    let Ok(name) = zbus::names::BusName::try_from(BUS_NAME) else {
        return false;
    };
    dbus.name_has_owner(name).await.unwrap_or(false)
}

async fn fetch(conn: &Connection) -> zbus::Result<Status> {
    let p = proxy(conn).await?;
    let json = p.get_history("", 0, POPUP_ROWS).await?;
    let unread = p.unread_count().await?;
    Ok(Status {
        online: true,
        // A daemon without UnseenCount (older build): fall back to the unread count.
        unseen: p.unseen_count().await.unwrap_or(unread),
        unread,
        dnd: p.do_not_disturb().await?,
        recent: parse_history(&json, POPUP_ROWS as usize),
    })
}

/// The daemon's current state; [`Status::offline`] when the name has no owner. A name that has an
/// owner but does not answer yet (it is still registering) gets one retry.
pub async fn snapshot(conn: &Connection) -> Status {
    if !has_owner(conn).await {
        return Status::offline();
    }
    if let Ok(s) = fetch(conn).await {
        return s;
    }
    tokio::time::sleep(Duration::from_millis(400)).await;
    fetch(conn).await.unwrap_or_default()
}

async fn signals(
    conn: &Connection,
    iface: &str,
    member: &str,
    arg0: Option<&str>,
) -> zbus::Result<MessageStream> {
    let mut b = MatchRule::builder()
        .msg_type(Type::Signal)
        .interface(iface.to_owned())?
        .member(member.to_owned())?
        .path(OBJECT_PATH)?;
    if let Some(a) = arg0 {
        b = b.add_arg(a)?;
    }
    // `for_match_rule` registers the rule on the bus, so build it before the first snapshot.
    MessageStream::for_match_rule(b.build(), conn, None).await
}

/// A stream of [`Status`]: the current one first, then a fresh one after every property change,
/// new notification or owner change of the daemon's bus name.
pub async fn updates(
    conn: &Connection,
) -> zbus::Result<Pin<Box<dyn Stream<Item = Status> + Send>>> {
    // Subscribe first, snapshot second: an event in between is then never lost.
    let props = signals(
        conn,
        "org.freedesktop.DBus.Properties",
        "PropertiesChanged",
        Some("net.corbinmeier.Notifications1"),
    )
    .await?;
    let added = signals(
        conn,
        "net.corbinmeier.Notifications1",
        "NotificationAdded",
        None,
    )
    .await?;
    let owner = zbus::fdo::DBusProxy::new(conn)
        .await?
        .receive_name_owner_changed_with_args(&[(0, BUS_NAME)])
        .await?;
    let first = snapshot(conn).await;
    let events = stream::select(
        stream::select(props.map(|_| ()), added.map(|_| ())),
        owner.map(|_| ()),
    );
    let conn = conn.clone();
    let later = events.then(move |()| {
        let conn = conn.clone();
        async move { snapshot(&conn).await }
    });
    Ok(Box::pin(stream::once(async move { first }).chain(later)))
}

/// Live round-trip against a real `bin/notifyd` on a private bus. Ignored by default:
///
/// ```text
/// dbus-run-session -- cargo test --manifest-path applet/Cargo.toml -- --ignored --nocapture
/// ```
#[cfg(test)]
mod private_bus {
    use std::collections::HashMap;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};

    use zbus::zvariant::Value;

    use super::*;

    const WAIT: Duration = Duration::from_secs(15);

    fn spawn_daemon(addr: &str, dir: &std::path::Path) -> Child {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
        Command::new(root.join("bin/notifyd"))
            .args([
                "--no-display",
                "--no-sound",
                "--no-run",
                "--bus-address",
                addr,
            ])
            .arg("--db")
            .arg(dir.join("t.db"))
            .arg("--config")
            .arg(dir.join("none.toml"))
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn bin/notifyd")
    }

    async fn next_where(
        s: &mut (impl Stream<Item = Status> + Unpin),
        what: &str,
        pred: impl Fn(&Status) -> bool,
    ) -> Status {
        let wait = async {
            while let Some(st) = s.next().await {
                println!("  status: {st:?}");
                if pred(&st) {
                    return st;
                }
            }
            panic!("stream ended waiting for {what}");
        };
        tokio::time::timeout(WAIT, wait)
            .await
            .unwrap_or_else(|_| panic!("timed out waiting for {what}"))
    }

    async fn notify(conn: &Connection, summary: &str) -> u32 {
        let hints: HashMap<String, Value<'_>> = HashMap::new();
        let reply = conn
            .call_method(
                Some("org.freedesktop.Notifications"),
                "/org/freedesktop/Notifications",
                Some("org.freedesktop.Notifications"),
                "Notify",
                &(
                    "proof-app",
                    0u32,
                    "",
                    summary,
                    "body",
                    Vec::<String>::new(),
                    hints,
                    -1i32,
                ),
            )
            .await
            .expect("Notify");
        reply.body().deserialize::<u32>().expect("id")
    }

    #[tokio::test(flavor = "multi_thread")]
    #[ignore = "needs a private bus: run under dbus-run-session"]
    async fn bell_state_follows_the_daemon() {
        let addr = std::env::var("DBUS_SESSION_BUS_ADDRESS").expect("run under dbus-run-session");
        let dir = std::env::temp_dir().join(format!("applet-proof-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();

        let conn = connect_to(&addr).await.unwrap();
        let mut ups = updates(&conn).await.unwrap();
        let mut added = signals(
            &conn,
            "net.corbinmeier.Notifications1",
            "NotificationAdded",
            None,
        )
        .await
        .unwrap();

        println!("1. no daemon yet");
        let st = next_where(&mut ups, "offline", |_| true).await;
        assert!(!st.online);
        assert_eq!(st.tooltip(), "Notification daemon not running");

        println!("2. daemon starts (owner change)");
        let mut daemon = spawn_daemon(&addr, &dir);
        let st = next_where(&mut ups, "online", |s| s.online).await;
        assert_eq!((st.unseen, st.unread, st.dnd), (0, 0, false));
        assert_eq!(st.icon(), crate::model::ICON_BELL);

        println!("3. a notification arrives (NotificationAdded + UnreadCount)");
        let id = notify(&conn, "Hello from the proof").await;
        let sig = tokio::time::timeout(WAIT, added.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap();
        let (sid, json): (u32, String) = sig.body().deserialize().unwrap();
        println!("  NotificationAdded id={sid} json={json}");
        assert_eq!(sid, id);
        let st = next_where(&mut ups, "unread 1", |s| s.unread == 1 && s.unseen == 1).await;
        assert_eq!(st.badge().as_deref(), Some("1"));
        assert_eq!(st.icon(), crate::model::ICON_BELL_NEW);
        assert_eq!(st.header(), "1 new \u{b7} 1 unread");
        assert_eq!(st.recent[0].summary, "Hello from the proof");
        assert!(st.recent[0].is_unread());
        assert_eq!(st.recent[0].label(), "proof-app: Hello from the proof");

        println!("4. DND round-trip");
        set_dnd(&conn, true).await.unwrap();
        let st = next_where(&mut ups, "dnd on", |s| s.dnd).await;
        assert_eq!(st.icon(), crate::model::ICON_BELL_DND);
        set_dnd(&conn, false).await.unwrap();
        next_where(&mut ups, "dnd off", |s| !s.dnd).await;

        println!("4b. MarkSeen: the badge drops to 0, unread stays");
        mark_seen(&conn).await.unwrap();
        let st = next_where(&mut ups, "seen", |s| s.unseen == 0).await;
        assert_eq!((st.badge(), st.unread), (None, 1));
        notify(&conn, "second").await;
        next_where(&mut ups, "unseen 1 again", |s| {
            s.unseen == 1 && s.unread == 2
        })
        .await;

        println!("5. window calls (opening the shade marks seen)");
        notify(&conn, "third").await;
        next_where(&mut ups, "unseen 2", |s| s.unseen == 2).await;
        // --no-display runs a NullShade that is never visible, so ShowShade (always marks) is
        // the call that proves the reset; ToggleShade is only checked to answer.
        show_shade(&conn).await.unwrap();
        next_where(&mut ups, "shade opened", |s| s.unseen == 0).await;
        toggle_shade(&conn).await.unwrap();
        show_shade(&conn).await.unwrap();
        toggle_shade_or_center(&conn).await.unwrap();
        toggle_center(&conn).await.unwrap();
        show_center(&conn).await.unwrap();
        show_notification(&conn, id).await.unwrap();

        println!("5b. an old daemon without ToggleShade: the bell falls back to ToggleCenter");
        let err = conn
            .call_method(
                Some(BUS_NAME),
                OBJECT_PATH,
                Some("net.corbinmeier.Notifications1"),
                "NoSuchMethod",
                &(),
            )
            .await
            .unwrap_err();
        match err {
            zbus::Error::MethodError(name, _, _) => assert!(is_missing_method(name.as_str())),
            other => panic!("unexpected error {other:?}"),
        }

        println!("6. daemon dies (owner change to none)");
        daemon.kill().unwrap();
        daemon.wait().unwrap();
        let st = next_where(&mut ups, "offline again", |s| !s.online).await;
        assert_eq!(st.tooltip(), "Notification daemon not running");

        println!("7. daemon restarts: the applet recovers on its own");
        let mut daemon = spawn_daemon(&addr, &dir);
        let st = next_where(&mut ups, "online again", |s| s.online).await;
        assert_eq!(st.recent[0].summary, "third");

        daemon.kill().unwrap();
        daemon.wait().unwrap();
        let _ = std::fs::remove_dir_all(&dir);
        println!("OK");
    }
}
