use std::process::{Command, Stdio};

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_rdp-session")
}

#[test]
fn help_exposes_only_create_command() {
    let output = Command::new(binary())
        .arg("--help")
        .output()
        .expect("run help");

    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("utf8 stdout");
    assert!(stdout.contains("create"));
    assert!(!stdout.contains("status"));
    assert!(!stdout.contains("ensure"));
}

#[test]
fn removed_status_and_ensure_commands_are_rejected() {
    for command in ["status", "ensure"] {
        let output = Command::new(binary())
            .arg(command)
            .output()
            .expect("run removed command");

        assert!(!output.status.success());
    }
}

#[test]
fn json_errors_go_to_stderr_with_empty_stdout() {
    let mut child = Command::new(binary())
        .args([
            "--output",
            "json",
            "create",
            "--host",
            "127.0.0.1",
            "--username",
            "user",
            "--password-stdin",
            "--password-env",
            "OTHER_PASSWORD_ENV",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn conflict command");

    std::io::Write::write_all(child.stdin.as_mut().expect("stdin"), b"secret\n")
        .expect("write stdin");

    let output = child.wait_with_output().expect("wait for command");
    assert_eq!(output.status.code(), Some(10));
    assert!(output.stdout.is_empty());

    let stderr = String::from_utf8(output.stderr).expect("utf8 stderr");
    assert!(stderr.contains("\"kind\":\"config\""));
    assert!(stderr.contains("use either --password-stdin or --password-env"));
}

#[test]
fn non_local_hosts_require_insecure_cert_consent_before_connecting() {
    let mut child = Command::new(binary())
        .args([
            "--output",
            "json",
            "create",
            "--host",
            "192.0.2.1",
            "--username",
            "user",
            "--password-stdin",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn non-local command");

    std::io::Write::write_all(child.stdin.as_mut().expect("stdin"), b"secret\n")
        .expect("write stdin");

    let output = child.wait_with_output().expect("wait for command");
    assert_eq!(output.status.code(), Some(10));
    assert!(output.stdout.is_empty());

    let stderr = String::from_utf8(output.stderr).expect("utf8 stderr");
    assert!(stderr.contains("\"kind\":\"config\""));
    assert!(stderr.contains("requires --allow-insecure-cert"));
}
