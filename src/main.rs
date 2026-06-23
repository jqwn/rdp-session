use std::env;
use std::fmt;
use std::io::{Read as _, Write as _};
use std::net::{IpAddr, TcpStream, ToSocketAddrs as _};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::mpsc;
use std::time::Duration;

use anyhow::{Context as _, anyhow};
use clap::{Args, Parser, Subcommand, ValueEnum};
use ironrdp::connector::{self, ConnectionResult, Credentials};
use ironrdp::pdu::gcc::KeyboardType;
use ironrdp::pdu::rdp::capability_sets::MajorPlatformType;
use ironrdp::session::image::DecodedImage;
use ironrdp::session::{ActiveStage, ActiveStageOutput};
use ironrdp_pdu::input::fast_path::{FastPathInputEvent, KeyboardFlags};
use ironrdp_pdu::input::mouse::{MousePdu, PointerFlags};
use ironrdp_pdu::rdp::client_info::{PerformanceFlags, TimezoneInfo};
use serde::Serialize;
use sspi::network_client::reqwest_network_client::ReqwestNetworkClient;
use tokio_rustls::rustls;
use tracing::{debug, info, trace};

const DEFAULT_PASSWORD_ENV: &str = "RDP_PASSWORD";

type UpgradedFramed =
    ironrdp_blocking::Framed<rustls::StreamOwned<rustls::ClientConnection, TcpStream>>;
type AppResult<T> = Result<T, AppError>;

#[derive(Debug, Parser)]
#[command(name = "rdp-session")]
#[command(version)]
#[command(about = "Create and detach a Windows RDP session with IronRDP")]
struct Cli {
    #[arg(long, value_enum, default_value_t = OutputMode::Text)]
    output: OutputMode,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum OutputMode {
    Text,
    Json,
}

#[derive(Debug, Subcommand)]
enum Commands {
    /// Create an RDP session and detach after the active stage settles.
    Create(RdpArgs),
}

#[derive(Clone, Debug, Args)]
struct RdpArgs {
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    #[arg(long, default_value_t = 3389)]
    port: u16,

    #[arg(long)]
    username: String,

    #[arg(long)]
    domain: Option<String>,

    /// Read the password from this environment variable. Defaults to RDP_PASSWORD.
    #[arg(long, value_name = "NAME")]
    password_env: Option<String>,

    /// Read the password from stdin, trimming only trailing CR/LF characters.
    #[arg(long)]
    password_stdin: bool,

    /// Permit IronRDP's insecure certificate verifier for non-local hosts.
    #[arg(long)]
    allow_insecure_cert: bool,

    #[arg(long, default_value_t = 1280)]
    desktop_width: u16,

    #[arg(long, default_value_t = 1024)]
    desktop_height: u16,

    #[arg(long, default_value_t = 30)]
    connect_timeout_seconds: u64,

    #[arg(long, default_value_t = 10)]
    read_timeout_seconds: u64,

    #[arg(long, default_value_t = 90)]
    operation_timeout_seconds: u64,

    #[arg(long, default_value_t = 3)]
    active_idle_seconds: u64,

    #[arg(long, default_value_t = 1)]
    min_active_frames: u64,

    #[arg(long, default_value_t = 1)]
    min_graphics_updates: u64,

    #[arg(long, default_value = "none", value_parser = parse_dismiss_action)]
    dismiss_action: DismissAction,

    #[arg(long)]
    screenshot: Option<PathBuf>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum DismissAction {
    None,
    Enter,
    Click { x: u16, y: u16 },
}

impl fmt::Display for DismissAction {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => f.write_str("none"),
            Self::Enter => f.write_str("enter"),
            Self::Click { x, y } => write!(f, "click:{x},{y}"),
        }
    }
}

impl DismissAction {
    fn input_events(&self) -> Vec<FastPathInputEvent> {
        match self {
            Self::None => Vec::new(),
            Self::Enter => vec![
                FastPathInputEvent::KeyboardEvent(KeyboardFlags::empty(), 0x1c),
                FastPathInputEvent::KeyboardEvent(KeyboardFlags::RELEASE, 0x1c),
            ],
            Self::Click { x, y } => vec![
                FastPathInputEvent::MouseEvent(MousePdu {
                    flags: PointerFlags::MOVE,
                    number_of_wheel_rotation_units: 0,
                    x_position: *x,
                    y_position: *y,
                }),
                FastPathInputEvent::MouseEvent(MousePdu {
                    flags: PointerFlags::LEFT_BUTTON | PointerFlags::DOWN,
                    number_of_wheel_rotation_units: 0,
                    x_position: *x,
                    y_position: *y,
                }),
                FastPathInputEvent::MouseEvent(MousePdu {
                    flags: PointerFlags::LEFT_BUTTON,
                    number_of_wheel_rotation_units: 0,
                    x_position: *x,
                    y_position: *y,
                }),
            ],
        }
    }
}

#[derive(Debug, Serialize)]
struct CreateReport {
    host: String,
    port: u16,
    username: String,
    domain: Option<String>,
    desktop_width: u16,
    desktop_height: u16,
    negotiated_width: u16,
    negotiated_height: u16,
    compression_type: String,
    active_frames: u64,
    graphics_updates: u64,
    response_frames: u64,
    terminated_by_server: bool,
    dismiss_action: String,
    dismiss_sent: bool,
    screenshot_path: Option<String>,
    screenshot_saved: bool,
    detached: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ExitKind {
    Unexpected,
    Config,
    Auth,
    Network,
    Timeout,
    DesktopProof,
    Protocol,
}

impl ExitKind {
    fn code(self) -> u8 {
        match self {
            Self::Unexpected => 1,
            Self::Config => 10,
            Self::Auth => 20,
            Self::Network => 21,
            Self::Timeout => 22,
            Self::DesktopProof => 23,
            Self::Protocol => 24,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Unexpected => "unexpected",
            Self::Config => "config",
            Self::Auth => "auth",
            Self::Network => "network",
            Self::Timeout => "timeout",
            Self::DesktopProof => "desktop_proof",
            Self::Protocol => "protocol",
        }
    }
}

#[derive(Debug)]
struct AppError {
    kind: ExitKind,
    source: anyhow::Error,
}

impl AppError {
    fn new(kind: ExitKind, source: anyhow::Error) -> Self {
        Self { kind, source }
    }

    fn message(&self) -> String {
        format!("{:#}", self.source)
    }
}

impl fmt::Display for AppError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message())
    }
}

fn main() -> ExitCode {
    let cli = Cli::parse();

    if let Err(err) = setup_logging() {
        eprintln!("failed to initialize logging: {err:#}");
        return ExitCode::from(ExitKind::Unexpected.code());
    }

    match run(&cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            if cli.output == OutputMode::Json {
                let error = serde_json::json!({
                    "ok": false,
                    "kind": err.kind.as_str(),
                    "exit_code": err.kind.code(),
                    "error": err.message(),
                });
                eprintln!("{error}");
            } else {
                eprintln!("{}", err.message());
            }
            ExitCode::from(err.kind.code())
        }
    }
}

fn run(cli: &Cli) -> AppResult<()> {
    match &cli.command {
        Commands::Create(args) => {
            let report = create_session(args)?;
            print_report(cli.output, &report, create_text(&report))
                .map_err(|err| AppError::new(ExitKind::Unexpected, err))
        }
    }
}

fn setup_logging() -> anyhow::Result<()> {
    use tracing::metadata::LevelFilter;
    use tracing_subscriber::EnvFilter;
    use tracing_subscriber::prelude::*;

    let fmt_layer = tracing_subscriber::fmt::layer()
        .compact()
        .with_writer(std::io::stderr);
    let env_filter = EnvFilter::builder()
        .with_default_directive(LevelFilter::WARN.into())
        .with_env_var("RDP_SESSION_LOG")
        .from_env_lossy();

    tracing_subscriber::registry()
        .with(fmt_layer)
        .with(env_filter)
        .try_init()
        .context("set tracing subscriber")?;

    Ok(())
}

fn print_report<T: Serialize>(mode: OutputMode, report: &T, text: String) -> anyhow::Result<()> {
    match mode {
        OutputMode::Text => {
            println!("{text}");
        }
        OutputMode::Json => {
            println!("{}", serde_json::to_string_pretty(report)?);
        }
    }

    Ok(())
}

fn create_session(args: &RdpArgs) -> AppResult<CreateReport> {
    let timeout = Duration::from_secs(args.operation_timeout_seconds);
    let args = args.clone();
    let (tx, rx) = mpsc::channel();

    std::thread::spawn(move || {
        let _ = tx.send(create_session_inner(&args));
    });

    match rx.recv_timeout(timeout) {
        Ok(result) => result,
        Err(mpsc::RecvTimeoutError::Timeout) => Err(AppError::new(
            ExitKind::Timeout,
            anyhow!(
                "RDP operation timed out after {} second(s)",
                timeout.as_secs()
            ),
        )),
        Err(mpsc::RecvTimeoutError::Disconnected) => Err(AppError::new(
            ExitKind::Unexpected,
            anyhow!("RDP worker ended without returning a result"),
        )),
    }
}

fn create_session_inner(args: &RdpArgs) -> AppResult<CreateReport> {
    validate_args(args)?;

    let password = read_password(args)?;
    let connector_config = build_config(args, password);
    let (connection_result, framed) = connect(connector_config, args).map_err(map_connect_error)?;
    let negotiated_width = connection_result.desktop_size.width;
    let negotiated_height = connection_result.desktop_size.height;
    let compression_type = format!("{:?}", connection_result.compression_type);

    let active_report = active_stage(
        connection_result,
        framed,
        Duration::from_secs(args.active_idle_seconds),
        &args.dismiss_action,
        args.screenshot.as_deref(),
    )
    .map_err(map_active_error)?;

    if active_report.active_frames < args.min_active_frames {
        return Err(AppError::new(
            ExitKind::DesktopProof,
            anyhow!(
                "desktop proof failed: received {} active-stage frame(s), minimum is {}",
                active_report.active_frames,
                args.min_active_frames
            ),
        ));
    }

    if active_report.graphics_updates < args.min_graphics_updates {
        return Err(AppError::new(
            ExitKind::DesktopProof,
            anyhow!(
                "desktop proof failed: received {} graphics update(s), minimum is {}",
                active_report.graphics_updates,
                args.min_graphics_updates
            ),
        ));
    }

    Ok(CreateReport {
        host: args.host.clone(),
        port: args.port,
        username: args.username.clone(),
        domain: args.domain.clone(),
        desktop_width: args.desktop_width,
        desktop_height: args.desktop_height,
        negotiated_width,
        negotiated_height,
        compression_type,
        active_frames: active_report.active_frames,
        graphics_updates: active_report.graphics_updates,
        response_frames: active_report.response_frames,
        terminated_by_server: active_report.terminated_by_server,
        dismiss_action: args.dismiss_action.to_string(),
        dismiss_sent: active_report.dismiss_sent,
        screenshot_path: active_report.screenshot_path,
        screenshot_saved: active_report.screenshot_saved,
        detached: !active_report.terminated_by_server,
    })
}

fn validate_args(args: &RdpArgs) -> AppResult<()> {
    if args.password_stdin && args.password_env.is_some() {
        return Err(AppError::new(
            ExitKind::Config,
            anyhow!("use either --password-stdin or --password-env, not both"),
        ));
    }

    if !is_local_host(&args.host) && !args.allow_insecure_cert {
        return Err(AppError::new(
            ExitKind::Config,
            anyhow!(
                "non-local host {} requires --allow-insecure-cert because certificate validation is not implemented",
                args.host
            ),
        ));
    }

    Ok(())
}

fn read_password(args: &RdpArgs) -> AppResult<String> {
    let password = if args.password_stdin {
        let mut password = String::new();
        std::io::stdin()
            .read_to_string(&mut password)
            .map_err(|err| {
                AppError::new(
                    ExitKind::Config,
                    anyhow!(err).context("read password from stdin"),
                )
            })?;
        trim_stdin_password(password)
    } else {
        let password_env = args.password_env.as_deref().unwrap_or(DEFAULT_PASSWORD_ENV);
        env::var(password_env).map_err(|err| {
            AppError::new(
                ExitKind::Config,
                anyhow!(err).context(format!(
                    "read password from environment variable {password_env}"
                )),
            )
        })?
    };

    if password.is_empty() {
        return Err(AppError::new(
            ExitKind::Config,
            anyhow!("password is empty"),
        ));
    }

    Ok(password)
}

fn trim_stdin_password(mut password: String) -> String {
    while password.ends_with('\n') || password.ends_with('\r') {
        password.pop();
    }

    password
}

fn build_config(args: &RdpArgs, password: String) -> connector::Config {
    connector::Config {
        credentials: Credentials::UsernamePassword {
            username: args.username.clone(),
            password,
        },
        domain: args.domain.clone(),
        enable_tls: false,
        enable_credssp: true,
        keyboard_type: KeyboardType::IbmEnhanced,
        keyboard_subtype: 0,
        keyboard_layout: 0,
        keyboard_functional_keys_count: 12,
        ime_file_name: String::new(),
        dig_product_id: String::new(),
        desktop_size: connector::DesktopSize {
            width: args.desktop_width,
            height: args.desktop_height,
        },
        bitmap: None,
        client_build: 0,
        client_name: "rdp-session".to_owned(),
        client_dir: "C:\\Windows\\System32\\mstscax.dll".to_owned(),
        platform: platform_type(),
        enable_server_pointer: false,
        request_data: None,
        autologon: false,
        enable_audio_playback: false,
        compression_type: Some(ironrdp_pdu::rdp::client_info::CompressionType::Rdp61),
        pointer_software_rendering: true,
        multitransport_flags: None,
        performance_flags: PerformanceFlags::default(),
        desktop_scale_factor: 0,
        hardware_id: None,
        license_cache: None,
        timezone_info: TimezoneInfo::default(),
        alternate_shell: String::new(),
        work_dir: String::new(),
    }
}

fn platform_type() -> MajorPlatformType {
    #[cfg(windows)]
    {
        MajorPlatformType::WINDOWS
    }

    #[cfg(target_os = "macos")]
    {
        MajorPlatformType::MACINTOSH
    }

    #[cfg(target_os = "ios")]
    {
        MajorPlatformType::IOS
    }

    #[cfg(target_os = "android")]
    {
        MajorPlatformType::ANDROID
    }

    #[cfg(any(
        target_os = "linux",
        target_os = "freebsd",
        target_os = "dragonfly",
        target_os = "openbsd",
        target_os = "netbsd",
        not(any(windows, target_os = "macos", target_os = "ios", target_os = "android"))
    ))]
    {
        MajorPlatformType::UNIX
    }
}

fn connect(
    config: connector::Config,
    args: &RdpArgs,
) -> anyhow::Result<(ConnectionResult, UpgradedFramed)> {
    let server_addr = lookup_addr(&args.host, args.port).context("lookup address")?;
    info!(%server_addr, "looked up server address");

    let tcp_stream = TcpStream::connect_timeout(
        &server_addr,
        Duration::from_secs(args.connect_timeout_seconds),
    )
    .context("TCP connect")?;
    tcp_stream
        .set_read_timeout(Some(Duration::from_secs(args.read_timeout_seconds)))
        .context("set TCP read timeout")?;
    tcp_stream
        .set_write_timeout(Some(Duration::from_secs(args.read_timeout_seconds)))
        .context("set TCP write timeout")?;

    let client_addr = tcp_stream
        .local_addr()
        .context("get local socket address")?;
    let mut framed = ironrdp_blocking::Framed::new(tcp_stream);
    let mut connector = connector::ClientConnector::new(config, client_addr);

    let should_upgrade =
        ironrdp_blocking::connect_begin(&mut framed, &mut connector).context("begin connection")?;
    let initial_stream = framed.into_inner_no_leftover();
    let (upgraded_stream, server_public_key) =
        tls_upgrade(initial_stream, args.host.clone()).context("TLS upgrade")?;
    let upgraded = ironrdp_blocking::mark_as_upgraded(should_upgrade, &mut connector);
    let mut upgraded_framed = ironrdp_blocking::Framed::new(upgraded_stream);
    let mut network_client = ReqwestNetworkClient;

    let connection_result = ironrdp_blocking::connect_finalize(
        upgraded,
        connector,
        &mut upgraded_framed,
        &mut network_client,
        args.host.clone().into(),
        server_public_key,
        None,
    )
    .context("finalize connection")?;

    Ok((connection_result, upgraded_framed))
}

#[derive(Debug)]
struct ActiveStageReport {
    active_frames: u64,
    graphics_updates: u64,
    response_frames: u64,
    terminated_by_server: bool,
    dismiss_sent: bool,
    screenshot_path: Option<String>,
    screenshot_saved: bool,
}

#[derive(Default)]
struct ActiveCounters {
    graphics_updates: u64,
    response_frames: u64,
    terminated_by_server: bool,
}

fn active_stage(
    connection_result: ConnectionResult,
    mut framed: UpgradedFramed,
    active_idle_timeout: Duration,
    dismiss_action: &DismissAction,
    screenshot_path: Option<&Path>,
) -> anyhow::Result<ActiveStageReport> {
    framed
        .get_inner_mut()
        .0
        .sock
        .set_read_timeout(Some(active_idle_timeout))
        .context("set active-stage read timeout")?;

    let mut image = DecodedImage::new(
        ironrdp_graphics::image_processing::PixelFormat::RgbA32,
        connection_result.desktop_size.width,
        connection_result.desktop_size.height,
    );
    let mut active_stage = ActiveStage::new(connection_result);
    let mut active_frames = 0;
    let mut dismiss_sent = false;
    let mut counters = ActiveCounters::default();

    loop {
        let (action, payload) = match framed.read_pdu() {
            Ok((action, payload)) => (action, payload),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                ) =>
            {
                break;
            }
            Err(e) => return Err(anyhow::Error::new(e).context("read active-stage frame")),
        };

        active_frames += 1;
        trace!(
            ?action,
            frame_length = payload.len(),
            "active-stage frame received"
        );

        let graphics_before = counters.graphics_updates;
        let outputs = active_stage.process(&mut image, action, &payload)?;
        if handle_active_outputs(outputs, &mut framed, &mut counters)? {
            break;
        }

        if !dismiss_sent && counters.graphics_updates > graphics_before {
            let events = dismiss_action.input_events();
            if !events.is_empty() {
                let outputs = active_stage.process_fastpath_input(&mut image, &events)?;
                dismiss_sent = true;
                if handle_active_outputs(outputs, &mut framed, &mut counters)? {
                    break;
                }
            }
        }
    }

    let screenshot_path = save_screenshot(screenshot_path, &image)?;

    Ok(ActiveStageReport {
        active_frames,
        graphics_updates: counters.graphics_updates,
        response_frames: counters.response_frames,
        terminated_by_server: counters.terminated_by_server,
        dismiss_sent,
        screenshot_saved: screenshot_path.is_some(),
        screenshot_path,
    })
}

fn handle_active_outputs(
    outputs: Vec<ActiveStageOutput>,
    framed: &mut UpgradedFramed,
    counters: &mut ActiveCounters,
) -> anyhow::Result<bool> {
    for output in outputs {
        match output {
            ActiveStageOutput::ResponseFrame(frame) => {
                counters.response_frames += 1;
                framed
                    .write_all(&frame)
                    .context("write active-stage response")?;
            }
            ActiveStageOutput::GraphicsUpdate(_) => {
                counters.graphics_updates += 1;
            }
            ActiveStageOutput::Terminate(_) => {
                counters.terminated_by_server = true;
                return Ok(true);
            }
            _ => {}
        }
    }

    Ok(false)
}

fn save_screenshot(path: Option<&Path>, image: &DecodedImage) -> anyhow::Result<Option<String>> {
    let Some(path) = path else {
        return Ok(None);
    };

    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        std::fs::create_dir_all(parent)
            .with_context(|| format!("create screenshot directory {}", parent.to_string_lossy()))?;
    }

    let img: image::ImageBuffer<image::Rgba<u8>, Vec<u8>> = image::ImageBuffer::from_raw(
        u32::from(image.width()),
        u32::from(image.height()),
        image.data().to_vec(),
    )
    .context("invalid screenshot image buffer")?;

    img.save(path)
        .with_context(|| format!("save screenshot to {}", path.to_string_lossy()))?;

    Ok(Some(path.to_string_lossy().into_owned()))
}

fn lookup_addr(hostname: &str, port: u16) -> anyhow::Result<std::net::SocketAddr> {
    (hostname, port)
        .to_socket_addrs()?
        .next()
        .context("socket address not found")
}

fn tls_upgrade(
    stream: TcpStream,
    server_name: String,
) -> anyhow::Result<(
    rustls::StreamOwned<rustls::ClientConnection, TcpStream>,
    Vec<u8>,
)> {
    let mut config = rustls::client::ClientConfig::builder()
        .dangerous()
        .with_custom_certificate_verifier(std::sync::Arc::new(danger::NoCertificateVerification))
        .with_no_client_auth();

    config.resumption = rustls::client::Resumption::disabled();

    let client =
        rustls::ClientConnection::new(std::sync::Arc::new(config), server_name.try_into()?)?;
    let mut tls_stream = rustls::StreamOwned::new(client, stream);
    tls_stream.flush()?;

    let cert = tls_stream
        .conn
        .peer_certificates()
        .and_then(|certificates| certificates.first())
        .context("peer certificate is missing")?;
    let server_public_key = extract_tls_server_public_key(cert)?;

    Ok((tls_stream, server_public_key))
}

fn extract_tls_server_public_key(cert: &[u8]) -> anyhow::Result<Vec<u8>> {
    use x509_cert::der::Decode as _;

    let cert = x509_cert::Certificate::from_der(cert)?;
    debug!(%cert.tbs_certificate.subject);

    cert.tbs_certificate
        .subject_public_key_info
        .subject_public_key
        .as_bytes()
        .context("subject public key BIT STRING is not aligned")
        .map(ToOwned::to_owned)
}

fn is_local_host(host: &str) -> bool {
    if host.eq_ignore_ascii_case("localhost") {
        return true;
    }

    host.parse::<IpAddr>().is_ok_and(|addr| addr.is_loopback())
}

fn parse_dismiss_action(value: &str) -> Result<DismissAction, String> {
    if value.eq_ignore_ascii_case("none") {
        return Ok(DismissAction::None);
    }

    if value.eq_ignore_ascii_case("enter") {
        return Ok(DismissAction::Enter);
    }

    let Some(coordinates) = value.strip_prefix("click:") else {
        return Err("valid values are none, enter, or click:x,y".to_owned());
    };
    let Some((x, y)) = coordinates.split_once(',') else {
        return Err("click action must use click:x,y".to_owned());
    };

    Ok(DismissAction::Click {
        x: x.parse()
            .map_err(|_| "click x must be 0-65535".to_owned())?,
        y: y.parse()
            .map_err(|_| "click y must be 0-65535".to_owned())?,
    })
}

fn map_connect_error(err: anyhow::Error) -> AppError {
    let message = format!("{err:#}").to_ascii_lowercase();
    let kind = if message.contains("status_logon_failure")
        || message.contains("logon failure")
        || message.contains("logon_denied")
        || message.contains("invalid username")
    {
        ExitKind::Auth
    } else if message.contains("timed out") {
        ExitKind::Timeout
    } else if message.contains("lookup address")
        || message.contains("tcp connect")
        || message.contains("connection refused")
        || message.contains("network")
        || message.contains("tls upgrade")
    {
        ExitKind::Network
    } else {
        ExitKind::Protocol
    };

    AppError::new(kind, err)
}

fn map_active_error(err: anyhow::Error) -> AppError {
    let message = format!("{err:#}").to_ascii_lowercase();
    let kind = if message.contains("screenshot") || message.contains("desktop proof") {
        ExitKind::DesktopProof
    } else if message.contains("timed out") {
        ExitKind::Timeout
    } else {
        ExitKind::Protocol
    };

    AppError::new(kind, err.context("active stage"))
}

fn create_text(report: &CreateReport) -> String {
    let mut text = format!(
        "created RDP session for {} on {}:{} and detached after {} active-stage frame(s), {} graphics update(s)",
        report.username, report.host, report.port, report.active_frames, report.graphics_updates
    );

    if report.dismiss_sent {
        text.push_str(&format!("; sent {}", report.dismiss_action));
    }

    if let Some(path) = &report.screenshot_path {
        text.push_str(&format!("; screenshot saved to {path}"));
    }

    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_dismiss_actions() {
        assert_eq!(parse_dismiss_action("none").unwrap(), DismissAction::None);
        assert_eq!(parse_dismiss_action("enter").unwrap(), DismissAction::Enter);
        assert_eq!(
            parse_dismiss_action("click:12,34").unwrap(),
            DismissAction::Click { x: 12, y: 34 }
        );
        assert!(parse_dismiss_action("click:12").is_err());
    }

    #[test]
    fn trims_only_stdin_line_endings() {
        assert_eq!(trim_stdin_password("secret\r\n".to_owned()), "secret");
        assert_eq!(trim_stdin_password("secret  \n".to_owned()), "secret  ");
    }

    #[test]
    fn detects_local_hosts() {
        assert!(is_local_host("localhost"));
        assert!(is_local_host("127.0.0.1"));
        assert!(is_local_host("::1"));
        assert!(!is_local_host("192.0.2.10"));
    }
}

mod danger {
    use tokio_rustls::rustls::client::danger::{
        HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier,
    };
    use tokio_rustls::rustls::{DigitallySignedStruct, Error, SignatureScheme, pki_types};

    #[derive(Debug)]
    pub(super) struct NoCertificateVerification;

    impl ServerCertVerifier for NoCertificateVerification {
        fn verify_server_cert(
            &self,
            _: &pki_types::CertificateDer<'_>,
            _: &[pki_types::CertificateDer<'_>],
            _: &pki_types::ServerName<'_>,
            _: &[u8],
            _: pki_types::UnixTime,
        ) -> Result<ServerCertVerified, Error> {
            Ok(ServerCertVerified::assertion())
        }

        fn verify_tls12_signature(
            &self,
            _: &[u8],
            _: &pki_types::CertificateDer<'_>,
            _: &DigitallySignedStruct,
        ) -> Result<HandshakeSignatureValid, Error> {
            Ok(HandshakeSignatureValid::assertion())
        }

        fn verify_tls13_signature(
            &self,
            _: &[u8],
            _: &pki_types::CertificateDer<'_>,
            _: &DigitallySignedStruct,
        ) -> Result<HandshakeSignatureValid, Error> {
            Ok(HandshakeSignatureValid::assertion())
        }

        fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
            vec![
                SignatureScheme::RSA_PKCS1_SHA1,
                SignatureScheme::ECDSA_SHA1_Legacy,
                SignatureScheme::RSA_PKCS1_SHA256,
                SignatureScheme::ECDSA_NISTP256_SHA256,
                SignatureScheme::RSA_PKCS1_SHA384,
                SignatureScheme::ECDSA_NISTP384_SHA384,
                SignatureScheme::RSA_PKCS1_SHA512,
                SignatureScheme::ECDSA_NISTP521_SHA512,
                SignatureScheme::RSA_PSS_SHA256,
                SignatureScheme::RSA_PSS_SHA384,
                SignatureScheme::RSA_PSS_SHA512,
                SignatureScheme::ED25519,
                SignatureScheme::ED448,
            ]
        }
    }
}
