"""Exercise installer phases without apt, root access, accounts, or networking."""

import os
from pathlib import Path
import pty
import shutil
import stat
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "setup-deb.sh"

# Real tools may only write into the test's temporary home. Individual tests
# replace additional commands before invoking phases that would affect a host.
LOCAL_USER = r'''
as_user() {
    HOME="$TARGET_HOME" XDG_CONFIG_HOME="$TARGET_HOME/.config" \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL="$TARGET_HOME/.gitconfig" \
        command "$@"
}
'''

# All run_setup phases that mutate a host are replaced. The real orchestrator
# can then demonstrate prompt ordering and stop-on-failure behavior safely.
SETUP_PHASES = r'''
phase() {
    printf 'event:%s\n' "$1"
    if [[ "${TEST_FAIL_PHASE:-}" == "$1" ]]; then
        return 37
    fi
}
select_user() { phase select; TARGET_USER=alice; TARGET_GROUP=alice; }
collect_git_identity() { phase collect; GIT_NAME=alice; GIT_EMAIL=alice@example.com; }
install_system_packages() { phase apt; }
create_user() { phase account; }
configure_git() { phase git; }
install_docker() { phase docker; }
install_user_tools() { phase tools; }
install_plugins() { phase plugins; }
configure_zsh() { phase zsh; }
cleanup_packages() { phase cleanup; }
verify_install() { phase verify; }
print_summary() { phase summary; }
mktemp() {
    [[ "$*" == '-d /tmp/setup-vps.XXXXXX' ]]
    printf '%s\n' "$TEST_DIRECTORY/work"
}
chown() { [[ "$*" == "alice:alice $TEST_DIRECTORY/work" ]]; }
rm() {
    [[ "$1" == -rf && "$2" == -- && "$3" == "$TEST_DIRECTORY/work" ]]
    command rm "$@"
}
'''

# Files and Zsh startup run for real; Docker and installed tool checks stay local
# stubs so verification failures can be tested without any system installation.
VERIFY_TOOLS = r'''
getent() {
    [[ "$*" == "passwd $TARGET_USER" ]]
    printf '%s:x:1001:1001::%s:%s\n' "$TARGET_USER" "$TARGET_HOME" "${TEST_LOGIN_SHELL:-/usr/bin/zsh}"
}
systemctl() { :; }
docker() { :; }
as_user() {
    if [[ "$1" == test ]]; then
        command "$@"
    elif [[ "$1" == zsh ]]; then
        shift
        HOME="$TARGET_HOME" ZDOTDIR="$TARGET_HOME" command zsh -d "$@"
    fi
}
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="setup-vps-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.home = self.directory / "selected home"
        self.home.mkdir()
        self.work = self.directory / "work"
        self.work.mkdir()

    def bash(self, body, input="", *, env=None, expected=0):
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
            and key not in {"SUDO_USER", "BASH_ENV", "ENV", "ZDOTDIR"}
        }
        environment.update(
            SETUP_SCRIPT=str(SCRIPT),
            TARGET_HOME=str(self.home),
            TARGET_USER="alice",
            WORK_DIR=str(self.work),
            TEST_DIRECTORY=str(self.directory),
            HOME=str(self.directory),
            LC_ALL="C",
        )
        if env:
            environment.update({key: str(value) for key, value in env.items()})
        result = subprocess.run(
            [
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                'set -Eeuo pipefail\nsource "$SETUP_SCRIPT"\nexec 3<&0\n' + body,
            ],
            input=input,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )
        if expected is not None:
            self.assertEqual(
                result.returncode,
                expected,
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
        return result

    def release(self, distro, version, codename="bookworm"):
        path = self.directory / "os-release"
        path.write_text(
            f"ID={distro}\nVERSION_ID={version}\nVERSION_CODENAME={codename}\n"
        )
        return path

    def test_failure_report_preserves_status_and_identifies_user_home_and_phase(self):
        result = self.bash(
            'report_failure 37 123 install_user_tools\nprintf unsafe',
            expected=37,
        )
        self.assertIn("Failed in install_user_tools at line 123", result.stderr)
        self.assertIn("alice", result.stderr)
        self.assertIn(str(self.home), result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def zsh_startup(self, *, omit=None):
        module = self.home / ".config/setup-vps/zshrc"
        module.parent.mkdir(parents=True, exist_ok=True)
        (self.home / ".zshrc").write_text('source "$HOME/.config/setup-vps/zshrc"\n')
        module.write_text("\n".join(
            f"function {function}() {{ :; }}"
            for function in ("z", "fzf-history-widget", "_zsh_highlight", "fzf-tab-complete", "_zsh_autosuggest_start")
            if function != omit
        ) + "\n")

    def test_piped_output_uses_no_ansi_codes(self):
        result = self.bash(
            r'''
log 'Installing tools'
run_tool bash -c 'printf "ordinary stdout\n"; printf "ordinary stderr\n" >&2'
''',
            env={"TERM": "xterm-256color", "NO_COLOR": ""},
        )
        self.assertEqual(result.stdout, "\n==> Installing tools\nordinary stdout\n")
        self.assertEqual(result.stderr, "ordinary stderr\n")
        self.assertNotIn("\033", result.stdout + result.stderr)

    def test_tty_color_output_respects_no_color(self):
        for no_color in ("", "1"):
            with self.subTest(no_color=no_color):
                master, slave = pty.openpty()
                try:
                    self.bash(
                        r'''
exec > "$TEST_TTY"
log 'Installing tools'
run_tool printf 'ordinary output\n'
''',
                        env={"TEST_TTY": os.ttyname(slave), "TERM": "xterm-256color", "NO_COLOR": no_color},
                    )
                    output = os.read(master, 4096).decode()
                    self.assertIn("Installing tools", output)
                    self.assertIn("ordinary output", output)
                    if no_color:
                        self.assertNotIn("\033", output)
                    else:
                        self.assertIn("\033[1;36m", output)
                        self.assertIn("\033[2;37m", output)
                finally:
                    os.close(master)
                    os.close(slave)

    def test_tool_renderer_dims_noise_and_highlights_warning_and_error_lines(self):
        result = self.bash(
            r'''
use_color() { return 0; }
emit() {
    printf '\033[32mregular output\033[0m\nWARNING: attention\nfatal: broken\n'
    printf 'W: stderr warning\nE: stderr error\n' >&2
}
run_tool emit
'''
        )
        self.assertEqual(result.stdout.splitlines(), [
            "\033[2;37mregular output\033[0m",
            "\033[33mWARNING: attention\033[0m",
            "\033[1;31mfatal: broken\033[0m",
            "\033[33mW: stderr warning\033[0m",
            "\033[1;31mE: stderr error\033[0m",
        ])
        self.assertEqual(result.stderr, "")

    def test_tool_failures_preserve_status_and_stop_setup_with_or_without_color(self):
        for colored in (False, True):
            with self.subTest(colored=colored):
                result = self.bash(
                    "use_color() { return " + ("0" if colored else "1") + "; }\n"
                    + "run_tool bash -c 'printf \"ERROR: unavailable\\n\" >&2; exit 37'\n"
                    + "printf 'unsafe continuation'",
                    expected=37,
                )
                self.assertIn("ERROR: unavailable", result.stdout + result.stderr)
                self.assertNotIn("unsafe continuation", result.stdout)
                if colored:
                    self.assertIn("\033[1;31m", result.stdout)

    def test_own_errors_stand_out_in_color(self):
        result = self.bash(
            "use_color() { return 0; }\ndie 'installation failed'", expected=1
        )
        self.assertEqual(result.stderr, "\033[1;31msetup-vps: installation failed\033[0m\n")

    def test_yes_no_defaults_case_insensitivity_and_invalid_input(self):
        for answer, expected in (("\n", "no"), ("n\n", "no"), ("NO\n", "no"),
                                 ("y\n", "yes"), ("YES\n", "yes")):
            with self.subTest(answer=answer):
                result = self.bash(
                    'if ask "Continue?"; then printf yes; else printf no; fi',
                    answer,
                )
                self.assertEqual(result.stdout, expected)
        result = self.bash(
            'if ask "Continue?"; then printf yes; else printf no; fi',
            "maybe\nyes\n",
        )
        self.assertEqual(result.stdout, "yes")
        self.assertIn("Please answer yes or no", result.stderr)

    def test_yes_no_eof_aborts_instead_of_selecting_a_default(self):
        result = self.bash(
            'if ask "Continue?"; then :; fi\nprintf unsafe', expected=1
        )
        self.assertIn("Input closed", result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def test_read_text_rejects_empty_values_and_preserves_literal_characters(self):
        result = self.bash(
            'value=old\nread_text "Git name" value\nprintf "<%s>" "$value"',
            "\n \t \n  Jane \\ Smith $HOME  \n",
        )
        self.assertEqual(result.stdout, "<Jane \\ Smith $HOME>")
        self.assertEqual(result.stderr.count("Enter a nonempty value"), 2)

    def test_read_text_eof_aborts(self):
        result = self.bash('read_text "Name" value\nprintf unsafe', expected=1)
        self.assertIn("Input closed", result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def test_supported_platforms_and_architectures(self):
        for distro, version, codename in (
            ("debian", "12", "bookworm"),
            ("debian", "13", "trixie"),
            ("ubuntu", "22.04", "jammy"),
            ("ubuntu", "24.04", "noble"),
            ("ubuntu", "26.04", "resolute"),
        ):
            for architecture in ("amd64", "arm64"):
                with self.subTest(distro=distro, version=version, arch=architecture):
                    release = self.release(distro, version, codename)
                    result = self.bash(
                        r'''
dpkg() { printf '%s\n' "$TEST_ARCH"; }
detect_platform "$TEST_RELEASE"
printf '%s:%s:%s' "$DISTRO" "$CODENAME" "$ARCH"
''',
                        env={"TEST_RELEASE": release, "TEST_ARCH": architecture},
                    )
                    self.assertEqual(
                        result.stdout, f"{distro}:{codename}:{architecture}"
                    )

    def test_unknown_distributions_releases_and_architectures_fail(self):
        for distro, version in (("debian", "11"), ("ubuntu", "20.04"), ("mint", "22")):
            with self.subTest(distro=distro, version=version):
                result = self.bash(
                    'dpkg() { printf amd64; }\ndetect_platform "$TEST_RELEASE"',
                    env={"TEST_RELEASE": self.release(distro, version)},
                    expected=1,
                )
                self.assertIn("Supported:", result.stderr)
        result = self.bash(
            'dpkg() { printf armhf; }\ndetect_platform "$TEST_RELEASE"',
            env={"TEST_RELEASE": self.release("debian", "12")},
            expected=1,
        )
        self.assertIn("Unsupported architecture: armhf", result.stderr)

    def test_root_can_decline_new_user_without_changing_accounts(self):
        result = self.bash(
            r'''
getent() { printf 'root:x:0:0:root:%s:/bin/bash\n' "$TARGET_HOME"; }
id() { printf root; }
adduser() { die 'unexpected account creation'; }
usermod() { die 'unexpected group modification'; }
select_user
[[ "$CREATE_USER" == 0 ]]
create_user
printf '\nselected=%s:%s:%s\n' "$TARGET_USER" "$TARGET_GROUP" "$TARGET_HOME"
''',
            "no\n",
        )
        self.assertIn(f"selected=root:root:{self.home}", result.stdout)

    def test_sudo_invocation_selects_original_user_without_prompting(self):
        result = self.bash(
            r'''
SUDO_USER=alice
getent() { [[ "$*" == 'passwd alice' ]]; printf 'alice:x:1001:1001::%s:/bin/bash\n' "$TARGET_HOME"; }
id() { [[ "$*" == '-gn alice' ]]; printf alice; }
ask() { die 'unexpected prompt'; }
adduser() { die 'unexpected account creation'; }
usermod() { die 'unexpected group modification'; }
select_user
[[ "$CREATE_USER" == 0 ]]
create_user
printf '\nselected=%s:%s\n' "$TARGET_USER" "$TARGET_GROUP"
'''
        )
        self.assertIn("selected=alice:alice", result.stdout)

    def test_new_user_validation_password_and_sudo_membership(self):
        result = self.bash(
            r'''
planned_home=$TARGET_HOME
unset TARGET_HOME
getent() {
    [[ "$1" == passwd ]]
    if [[ "$2" == taken ]]; then
        printf 'taken:x:1000:1000::/home/taken:/bin/bash\n'
    elif [[ "$2" == alice && -e "$WORK_DIR/created" ]]; then
        printf 'alice:x:1001:1001::%s:/bin/bash\n' "$planned_home"
    else
        return 2
    fi
}
adduser() { printf 'adduser:%s\n' "$*"; touch "$WORK_DIR/created"; }
passwd() { printf 'passwd:%s\n' "$*"; }
usermod() { printf 'usermod:%s\n' "$*"; }
id() { printf alice; }
select_user
[[ "$CREATE_USER" == 1 && "$TARGET_USER" == alice && ! -v TARGET_HOME ]]
[[ ! -e "$WORK_DIR/created" ]]
printf 'account is deferred\n'
create_user
printf '\nselected=%s\n' "$TARGET_USER"
''',
            "yes\nBadName\n_alice\ntaken\nalice\n",
        )
        self.assertEqual(result.stderr.count("Use a lowercase username"), 2)
        self.assertIn("That user already exists", result.stderr)
        self.assertLess(result.stdout.index("account is deferred"), result.stdout.index("adduser:"))
        self.assertIn("adduser:--disabled-password --gecos  alice", result.stdout)
        self.assertIn("passwd:alice", result.stdout)
        self.assertIn("usermod:-aG sudo alice", result.stdout)
        self.assertIn("selected=alice", result.stdout)

    def test_new_user_and_git_questions_do_not_require_home_or_write_accounts(self):
        result = self.bash(
            r'''
unset TARGET_HOME TARGET_GROUP
getent() { [[ "$*" == 'passwd alice' ]]; return 2; }
adduser() { die 'unexpected account creation'; }
passwd() { die 'unexpected password prompt'; }
usermod() { die 'unexpected group modification'; }
apt-get() { die 'unexpected package installation'; }
as_user() { die 'unexpected command for a prospective account'; }
select_user
collect_git_identity
[[ "$CREATE_USER" == 1 && "$TARGET_USER" == alice && ! -v TARGET_HOME ]]
printf 'identity=%s:%s\n' "$GIT_NAME" "$GIT_EMAIL"
read -r untouched <&3
printf 'remaining=%s\n' "$untouched"
''',
            "yes\nalice\nalice@example.com\nsentinel\n",
        )
        self.assertIn("identity=alice:alice@example.com", result.stdout)
        self.assertIn("remaining=sentinel", result.stdout)
        self.assertEqual(list(self.home.iterdir()), [])

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_missing_git_name_uses_root_or_original_sudo_username(self):
        for sudo_user, username, input in (("", "root", "no\n"), ("operator", "operator", "")):
            with self.subTest(sudo_user=sudo_user):
                result = self.bash(
                    LOCAL_USER
                    + r'''
getent() { printf '%s:x:1001:1001::%s:/bin/bash\n' "$2" "$TARGET_HOME"; }
id() { printf '%s' "$TARGET_USER"; }
adduser() { die 'unexpected account creation'; }
passwd() { die 'unexpected password prompt'; }
usermod() { die 'unexpected group modification'; }
apt-get() { die 'unexpected package installation'; }
select_user
collect_git_identity
printf 'identity=%s:%s\n' "$GIT_NAME" "$GIT_EMAIL"
read -r untouched <&3
printf 'remaining=%s\n' "$untouched"
''',
                    input + "shared@example.com\nsentinel\n",
                    env={"SUDO_USER": sudo_user},
                )
                self.assertIn(f"identity={username}:shared@example.com", result.stdout)
                self.assertIn("remaining=sentinel", result.stdout)
                self.assertEqual(list(self.home.iterdir()), [])

    def test_invalid_user_home_stops_before_configuration(self):
        result = self.bash(
            r'''
SUDO_USER=alice
getent() { printf 'alice:x:1001:1001::relative/home:/bin/bash\n'; }
select_user
printf unsafe
''',
            expected=1,
        )
        self.assertIn("Invalid or missing home", result.stderr)
        self.assertNotIn("unsafe", result.stdout)

    def test_fetch_prefers_fastfetch_and_falls_back_to_neofetch(self):
        for available, expected in (("fastfetch neofetch", "fastfetch"), ("neofetch", "neofetch")):
            with self.subTest(available=available):
                result = self.bash(
                    r'''
apt-cache() {
    if [[ " $TEST_AVAILABLE " == *" $2 "* ]]; then
        printf '  Candidate: 1.2.3\n'
    else
        printf '  Installed: (none)\n  Candidate: (none)\n'
    fi
}
select_fetch
printf '%s' "$FETCH_TOOL"
''',
                    env={"TEST_AVAILABLE": available},
                )
                self.assertEqual(result.stdout, expected)

    def test_fetch_missing_both_candidates_fails(self):
        result = self.bash(
            r'''
apt-cache() { printf '  Installed: 1.0\n  Candidate: (none)\n'; }
select_fetch
''',
            expected=1,
        )
        self.assertIn("Neither fastfetch nor neofetch", result.stderr)

    def test_package_candidate_lookup_uses_stable_locale(self):
        self.bash(
            r'''
unset LC_ALL
apt-cache() {
    [[ "$LC_ALL" == C ]] || return 8
    printf '  Candidate: 1.0\n'
}
package_available fastfetch
'''
        )

    def test_install_verification_accepts_neofetch_with_nonzero_version_status(self):
        result = self.bash(
            r'''
FETCH_TOOL=neofetch
getent() { printf 'alice:x:1001:1001::%s:/usr/bin/zsh\n' "$TARGET_HOME"; }
systemctl() { :; }
docker() { :; }
as_user() {
    if [[ "$1" == neofetch ]]; then
        [[ "$2" != --version ]] || return 1
        [[ "$2" == --stdout ]] || return 2
        printf 'OS: Debian\n'
    fi
}
verify_install
'''
        )
        self.assertIn("OS: Debian", result.stdout)

    def test_wrong_login_shell_prevents_success(self):
        self.zsh_startup()
        result = self.bash(
            VERIFY_TOOLS + "verify_install\nprint_summary",
            env={"TEST_LOGIN_SHELL": "/bin/bash", "FETCH_TOOL": "fastfetch"},
            expected=1,
        )
        self.assertIn("Default shell for alice is not /usr/bin/zsh", result.stderr)
        self.assertNotIn("Setup complete", result.stdout)

    def test_missing_zsh_startup_files_prevent_success(self):
        for relative, message in ((".zshrc", "Missing Zsh startup file"), (".config/setup-vps/zshrc", "Missing Zsh config")):
            with self.subTest(relative=relative):
                self.zsh_startup()
                missing = self.home / relative
                missing.unlink()
                result = self.bash(
                    VERIFY_TOOLS + "verify_install\nprint_summary",
                    env={"FETCH_TOOL": "fastfetch"},
                    expected=1,
                )
                self.assertIn(f"{message}: {missing}", result.stderr)
                self.assertNotIn("Setup complete", result.stdout)

    @unittest.skipUnless(shutil.which("zsh"), "Zsh is required")
    def test_missing_zsh_integrations_fail_real_startup_verification(self):
        for function in ("z", "fzf-history-widget", "_zsh_highlight", "fzf-tab-complete", "_zsh_autosuggest_start"):
            with self.subTest(function=function):
                self.zsh_startup(omit=function)
                result = self.bash(
                    VERIFY_TOOLS + "verify_install\nprint_summary",
                    env={"FETCH_TOOL": "fastfetch"},
                    expected=1,
                )
                self.assertIn("Zsh startup failed for alice", result.stderr)
                self.assertIn(str(self.home / ".zshrc"), result.stderr)
                self.assertIn(str(self.home / ".config/setup-vps/zshrc"), result.stderr)
                self.assertNotIn("Setup complete", result.stdout)

    @unittest.skipUnless(shutil.which("zsh"), "Zsh is required")
    def test_valid_selected_user_zsh_startup_passes_verification(self):
        self.zsh_startup()
        result = self.bash(
            VERIFY_TOOLS + "verify_install\nprint_summary",
            env={"FETCH_TOOL": "fastfetch"},
        )
        self.assertIn("Setup complete", result.stdout)
        self.assertIn(f"Zsh startup file: {self.home}/.zshrc", result.stdout)

    def test_summary_identifies_selected_home_and_new_login_command(self):
        for username in ("alice", "root"):
            with self.subTest(username=username):
                result = self.bash(
                    "print_summary",
                    env={"TARGET_USER": username, "FETCH_TOOL": "fastfetch"},
                )
                self.assertIn(f"Configured user: {username}\n", result.stdout)
                self.assertIn(f"Home: {self.home}\n", result.stdout)
                self.assertIn(f"Zsh startup file: {self.home}/.zshrc\n", result.stdout)
                self.assertIn(f"sudo -iu {username}\n", result.stdout)
                self.assertIn("current terminal keeps its shell", result.stdout)
                self.assertNotIn(f"Zsh startup file: {self.directory}/.zshrc", result.stdout)

    def test_system_install_updates_upgrades_and_installs_selected_fetch(self):
        result = self.bash(
            r'''
apt-get() {
    printf 'apt:%s;frontend=%s;restart=%s\n' "$*" "${DEBIAN_FRONTEND:-}" "${NEEDRESTART_MODE:-}"
}
package_available() { [[ "$1" == neofetch ]]; }
install_system_packages
'''
        )
        lines = [line for line in result.stdout.splitlines() if line.startswith("apt:")]
        self.assertEqual(len(lines), 3)
        self.assertIn(" update;", lines[0])
        self.assertIn("--force-confold upgrade -y", lines[1])
        self.assertIn("frontend=noninteractive;restart=a", lines[1])
        self.assertIn(" install -y ", lines[2])
        for package in ("sudo", "git", "zsh", "kitty-terminfo", "bat", "zoxide", "neofetch"):
            self.assertIn(package, lines[2].split(";")[0].split())
        self.assertNotIn("fastfetch", lines[2])

    def test_final_cleanup_is_noninteractive_and_failure_prevents_success_output(self):
        for status in (0, 42):
            with self.subTest(status=status):
                result = self.bash(
                    r'''
apt-get() {
    [[ "$DEBIAN_FRONTEND" == noninteractive ]] || exit 9
    [[ "$*" == '-o DPkg::Lock::Timeout=120 autoremove -y' ]] || exit 10
    printf 'cleanup attempted\n'
    return "$TEST_APT_STATUS"
}
verify_install() { printf 'verification after cleanup\n'; }
cleanup_packages
verify_install
print_summary
''',
                    env={"TEST_APT_STATUS": status, "FETCH_TOOL": "fastfetch"},
                    expected=status,
                )
                self.assertIn("cleanup attempted", result.stdout)
                self.assertEqual("verification after cleanup" in result.stdout, status == 0)
                self.assertEqual("Setup complete" in result.stdout, status == 0)
                if status == 0:
                    self.assertLess(result.stdout.index("cleanup attempted"), result.stdout.index("verification after cleanup"))

    def test_setup_collects_choices_before_install_and_completes_after_verification(self):
        result = self.bash(SETUP_PHASES + "run_setup")
        events = [line.removeprefix("event:") for line in result.stdout.splitlines()]
        for earlier, later in (
            ("select", "collect"),
            ("collect", "apt"),
            ("apt", "account"),
            ("account", "git"),
            ("git", "docker"),
            ("zsh", "cleanup"),
            ("cleanup", "verify"),
            ("verify", "summary"),
        ):
            with self.subTest(earlier=earlier, later=later):
                self.assertLess(events.index(earlier), events.index(later))
        self.assertEqual(events[-1], "summary")
        self.assertFalse(self.work.exists())

    def test_cleanup_or_verification_failure_prevents_success(self):
        for failing_phase in ("cleanup", "verify"):
            with self.subTest(failing_phase=failing_phase):
                self.work.mkdir(exist_ok=True)
                result = self.bash(
                    SETUP_PHASES + "run_setup",
                    env={"TEST_FAIL_PHASE": failing_phase},
                    expected=37,
                )
                events = [line.removeprefix("event:") for line in result.stdout.splitlines()]
                self.assertIn(failing_phase, events)
                self.assertNotIn("summary", events)
                if failing_phase == "cleanup":
                    self.assertNotIn("verify", events)
                self.assertFalse(self.work.exists())

    def test_docker_conflicts_only_block_installed_packages(self):
        result = self.bash(
            r'''
dpkg-query() {
    case "${@: -1}" in
        docker.io) printf config-files ;;
        docker-compose) printf not-installed ;;
        *) return 1 ;;
    esac
}
check_docker_conflicts
printf clear
'''
        )
        self.assertEqual(result.stdout, "clear")
        result = self.bash(
            r'''
dpkg-query() {
    case "${@: -1}" in
        docker.io|podman-docker|containerd) printf installed ;;
        *) return 1 ;;
    esac
}
check_docker_conflicts
''',
            expected=1,
        )
        self.assertIn("docker.io podman-docker containerd", result.stderr)
        self.assertIn("Remove them deliberately", result.stderr)

    def test_docker_repository_key_packages_and_optional_group_membership(self):
        for distro, codename, architecture, user, answer in (
            ("debian", "trixie", "amd64", "root", ""),
            ("ubuntu", "noble", "arm64", "alice", "yes\n"),
        ):
            with self.subTest(distro=distro, user=user):
                result = self.bash(
                    r'''
install() { printf 'install:%s\n' "$*"; }
curl() {
    [[ "$*" == *"https://download.docker.com/linux/$DISTRO/gpg"* ]]
    printf 'test signing key\n' > "$WORK_DIR/docker.asc"
}
write_system_file() {
    case "$1" in
        /etc/apt/keyrings/docker.asc) cat > "$WORK_DIR/captured-key" ;;
        /etc/apt/sources.list.d/docker.sources) cat > "$WORK_DIR/captured-sources" ;;
        *) die "unexpected destination: $1" ;;
    esac
}
apt-get() { printf 'apt:%s\n' "$*"; }
apt_install() { printf 'packages:%s\n' "$*"; }
systemctl() { printf 'systemctl:%s\n' "$*"; }
usermod() { printf 'usermod:%s\n' "$*"; }
install_docker
cat "$WORK_DIR/captured-sources"
''',
                    answer,
                    env={"DISTRO": distro, "CODENAME": codename, "ARCH": architecture, "TARGET_USER": user},
                )
                self.assertEqual((self.work / "captured-key").read_text(), "test signing key\n")
                self.assertIn(f"URIs: https://download.docker.com/linux/{distro}\n", result.stdout)
                self.assertIn(f"Suites: {codename}\n", result.stdout)
                self.assertIn(f"Architectures: {architecture}\n", result.stdout)
                self.assertIn("Signed-By: /etc/apt/keyrings/docker.asc\n", result.stdout)
                self.assertIn("packages:docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin", result.stdout)
                self.assertIn("systemctl:enable --now docker.service", result.stdout)
                if user == "root":
                    self.assertNotIn("usermod:", result.stdout)
                else:
                    self.assertIn("usermod:-aG docker alice", result.stdout)

    def test_empty_docker_key_never_changes_repository_or_installs_packages(self):
        result = self.bash(
            r'''
DISTRO=debian
install() { :; }
curl() { : > "$WORK_DIR/docker.asc"; }
write_system_file() { die 'unexpected repository write'; }
apt-get() { die 'unexpected apt'; }
apt_install() { die 'unexpected install'; }
install_docker
''',
            expected=1,
        )
        self.assertIn("Downloaded Docker signing key is empty", result.stderr)
        self.assertNotIn("unexpected", result.stderr)

    def test_selected_user_environment_excludes_caller_credentials_and_project(self):
        result = self.bash(
            r'''
runuser() {
    [[ "$1" == -u && "$2" == alice && "$3" == -- ]]
    shift 3
    command "$@"
}
export GIT_CONFIG_GLOBAL=/caller/credentials SSH_AUTH_SOCK=/caller/agent
export XDG_CONFIG_HOME=/caller/config AWS_SECRET_ACCESS_KEY=caller-secret
export MISE_TRUSTED_CONFIG_PATHS=/caller/project
cd "$WORK_DIR"
as_user bash -c 'printf "working=%s\n" "$PWD"; env'
'''
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(values["working"], str(self.home))
        self.assertEqual(values["HOME"], str(self.home))
        self.assertEqual(values["USER"], "alice")
        self.assertEqual(values["LOGNAME"], "alice")
        self.assertEqual(values["XDG_CONFIG_HOME"], str(self.home / ".config"))
        self.assertEqual(values["XDG_DATA_HOME"], str(self.home / ".local/share"))
        self.assertEqual(values["XDG_CACHE_HOME"], str(self.home / ".cache"))
        self.assertTrue(values["PATH"].startswith(str(self.home / ".local/bin") + ":"))
        for key in ("GIT_CONFIG_GLOBAL", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "MISE_TRUSTED_CONFIG_PATHS"):
            self.assertNotIn(key, values)

    def test_failed_mise_download_never_executes_partial_installer(self):
        self.bash(
            r'''
as_user() {
    case "$1" in
        mkdir) command "$@" ;;
        curl) printf 'partial download' > "$WORK_DIR/mise-install.sh"; return 22 ;;
        sh) touch "$WORK_DIR/executed" ;;
        *) die 'unexpected command after failed download' ;;
    esac
}
install_user_tools
''',
            expected=22,
        )
        self.assertFalse((self.work / "executed").exists())

    def test_failed_plugin_clone_keeps_previous_managed_plugins(self):
        plugins = self.home / ".local/share/setup-vps/plugins"
        plugins.mkdir(parents=True)
        marker = plugins / "previous-plugin"
        marker.write_text("working installation")
        self.bash(
            r'''
as_user() {
    case "$1" in
        mkdir) command "$@" ;;
        git) return 128 ;;
        *) die 'unexpected replacement after failed clone' ;;
    esac
}
install_plugins
''',
            expected=128,
        )
        self.assertEqual(marker.read_text(), "working installation")

    def test_plugin_rerun_replaces_the_managed_export(self):
        plugins = self.home / ".local/share/setup-vps/plugins"
        plugins.mkdir(parents=True)
        (plugins / "obsolete").write_text("stale")
        self.bash(
            r'''
as_user() {
    if [[ "$1" == git ]]; then
        mkdir -p -- "${@: -1}"
    else
        command "$@"
    fi
}
install_plugins
'''
        )
        self.assertEqual(
            sorted(path.name for path in plugins.iterdir()),
            ["fzf-tab", "zsh-autosuggestions", "zsh-completions", "zsh-syntax-highlighting"],
        )

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_existing_global_git_identity_does_not_prompt_or_rewrite(self):
        original = '[user]\n\tname = Existing Name\n\temail = old@example.com\n# keep me\n'
        config = self.home / ".gitconfig"
        config.write_text(original)
        result = self.bash(
            LOCAL_USER
            + "CREATE_USER=0\ncollect_git_identity\nconfigure_git\n"
            + "printf 'identity=%s:%s\\n' \"$GIT_NAME\" \"$GIT_EMAIL\""
        )
        self.assertIn("identity=Existing Name:old@example.com", result.stdout)
        self.assertEqual(config.read_text(), original)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_missing_git_name_uses_selected_username_without_prompting(self):
        (self.home / ".gitconfig").write_text('[user]\n\temail = old@example.com\n')
        result = self.bash(
            LOCAL_USER
            + r'''
CREATE_USER=0
collect_git_identity
if as_user git config --global --get user.name; then
    die 'collection unexpectedly wrote the name'
fi
configure_git
printf 'name=%s\n' "$(as_user git config --global --get user.name)"
printf 'email=%s\n' "$(as_user git config --global --get user.email)"
read -r untouched <&3
printf 'remaining=%s\n' "$untouched"
''',
            "sentinel\n",
        )
        self.assertIn("name=alice", result.stdout)
        self.assertIn("email=old@example.com", result.stdout)
        self.assertIn("remaining=sentinel", result.stdout)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_only_prompts_for_missing_email_and_preserves_existing_name(self):
        (self.home / ".gitconfig").write_text('[user]\n\tname = Existing Name\n')
        result = self.bash(
            LOCAL_USER
            + r'''
CREATE_USER=0
collect_git_identity
if as_user git config --global --get user.email; then
    die 'collection unexpectedly wrote the email'
fi
configure_git
printf 'name=%s\n' "$(as_user git config --global --get user.name)"
printf 'email=%s\n' "$(as_user git config --global --get user.email)"
read -r untouched <&3
printf 'remaining=%s\n' "$untouched"
''',
            "\nnew@example.com\nsentinel\n",
        )
        self.assertIn("name=Existing Name", result.stdout)
        self.assertIn("email=new@example.com", result.stdout)
        self.assertIn("remaining=sentinel", result.stdout)
        self.assertIn("Enter a nonempty value", result.stderr)

    def test_collection_works_without_git_or_runuser_and_performs_no_writes(self):
        for missing in ("git", "runuser"):
            with self.subTest(missing=missing):
                result = self.bash(
                    r'''
CREATE_USER=0
command() {
    if [[ "$1" == -v && "$2" == "$TEST_MISSING_TOOL" ]]; then
        return 1
    fi
    builtin command "$@"
}
as_user() { die 'unexpected user command before required tools exist'; }
adduser() { die 'unexpected account creation'; }
usermod() { die 'unexpected group modification'; }
apt-get() { die 'unexpected apt write'; }
collect_git_identity
printf 'identity=%s:%s\n' "$GIT_NAME" "$GIT_EMAIL"
read -r untouched <&3
printf 'remaining=%s\n' "$untouched"
''',
                    "early@example.com\nsentinel\n",
                    env={"TEST_MISSING_TOOL": missing},
                )
                self.assertIn("identity=alice:early@example.com", result.stdout)
                self.assertIn("remaining=sentinel", result.stdout)
                self.assertEqual(list(self.home.iterdir()), [])

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_identity_created_while_installing_takes_precedence(self):
        original = '[user]\nname = Added During Install\nemail = late@example.com\n# preserve this\n'
        result = self.bash(
            LOCAL_USER
            + r'''
CREATE_USER=0
collect_git_identity
printf '%s' "$TEST_LATE_CONFIG" > "$TARGET_HOME/.gitconfig"
configure_git
printf 'name=%s\n' "$(as_user git config --global --get user.name)"
printf 'email=%s\n' "$(as_user git config --global --get user.email)"
''',
            "early@example.com\n",
            env={"TEST_LATE_CONFIG": original},
        )
        self.assertIn("name=Added During Install", result.stdout)
        self.assertIn("email=late@example.com", result.stdout)
        self.assertEqual((self.home / ".gitconfig").read_text(), original)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_git_missing_before_install_preserves_identity_when_git_becomes_available(self):
        original = '[user]\nname = Existing Name\nemail = old@example.com\n'
        (self.home / ".gitconfig").write_text(original)
        result = self.bash(
            LOCAL_USER
            + r'''
CREATE_USER=0
command() {
    if [[ "$*" == '-v git' ]]; then
        return 1
    fi
    builtin command "$@"
}
collect_git_identity
unset -f command
configure_git
printf 'name=%s\n' "$(as_user git config --global --get user.name)"
printf 'email=%s\n' "$(as_user git config --global --get user.email)"
''',
            "early@example.com\n",
        )
        self.assertIn("name=Existing Name", result.stdout)
        self.assertIn("email=old@example.com", result.stdout)
        self.assertEqual((self.home / ".gitconfig").read_text(), original)

    @unittest.skipUnless(shutil.which("git"), "git is required")
    def test_malformed_git_config_is_an_error_instead_of_an_identity_prompt(self):
        config = self.home / ".gitconfig"
        original = '[user\nname = damaged\n'
        config.write_text(original)
        for phase in ("collect_git_identity", "configure_git"):
            with self.subTest(phase=phase):
                result = self.bash(
                    LOCAL_USER
                    + "CREATE_USER=0\nGIT_NAME=alice\nGIT_EMAIL=early@example.com\n"
                    + phase,
                    "unexpected input\n",
                    expected=1,
                )
                self.assertIn("Cannot read Git config", result.stderr)
                self.assertEqual(config.read_text(), original)

    def test_atomic_writes_replace_symlinks_and_do_not_touch_their_targets(self):
        for function in ("write_user_file", "write_system_file"):
            with self.subTest(function=function):
                folder = self.home / function
                folder.mkdir()
                original = folder / "unrelated"
                original.write_text("unrelated content\n")
                destination = folder / "managed config"
                destination.symlink_to(original)
                self.bash(
                    LOCAL_USER
                    + r'''
printf 'first version\n' | "$TEST_WRITER" "$TEST_DESTINATION"
printf 'replacement\n' | "$TEST_WRITER" "$TEST_DESTINATION"
''',
                    env={"TEST_WRITER": function, "TEST_DESTINATION": destination},
                )
                self.assertFalse(destination.is_symlink())
                self.assertEqual(destination.read_text(), "replacement\n")
                self.assertEqual(original.read_text(), "unrelated content\n")
                self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)
                self.assertEqual(sorted(p.name for p in folder.iterdir()), ["managed config", "unrelated"])

    @unittest.skipUnless(shutil.which("zsh"), "Zsh is required")
    def test_generated_zsh_loads_integrations_and_aliases_after_repeated_setup(self):
        binaries = self.home / ".local/bin"
        binaries.mkdir(parents=True)
        programs = {
            "mise": r'''#!/bin/sh
case "$1" in
    completion) printf '# generated completion\n' ;;
    activate) printf 'print -r -- mise >> "$SETUP_TEST_TRACE"\n' ;;
    *) exit 2 ;;
esac
''',
            "zoxide": r'''#!/bin/sh
printf 'function z() { builtin cd "$@"; }; function zi() { :; }; print -r -- zoxide >> "$SETUP_TEST_TRACE"\n'
''',
            "fzf": r'''#!/bin/sh
printf 'function fzf-history-widget() { :; }; zle -N fzf-history-widget; print -r -- fzf >> "$SETUP_TEST_TRACE"\n'
''',
            "starship": r'''#!/bin/sh
printf 'print -r -- starship >> "$SETUP_TEST_TRACE"\n'
''',
        }
        for name, content in programs.items():
            path = binaries / name
            path.write_text(content)
            path.chmod(0o755)
        plugins = self.home / ".local/share/setup-vps/plugins"
        plugin_sources = {
            "fzf-tab/fzf-tab.plugin.zsh": r'''
(( ${+functions[_main_complete]} && ${+functions[fzf-history-widget]} )) || return 9
print -r -- fzf-tab >> "$SETUP_TEST_TRACE"
''',
            "zsh-autosuggestions/zsh-autosuggestions.zsh": 'print -r -- autosuggestions >> "$SETUP_TEST_TRACE"\n',
            "zsh-syntax-highlighting/zsh-syntax-highlighting.zsh": 'function _zsh_highlight() { :; }; print -r -- highlighting >> "$SETUP_TEST_TRACE"\n',
            "zsh-completions/src/_fixture": '#compdef fixture\n_fixture() { :; }\n',
        }
        for name, content in plugin_sources.items():
            path = plugins / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        trace = self.work / "integration trace"
        destination = self.home / "directory with spaces"
        destination.mkdir()
        result = self.bash(
            LOCAL_USER
            + r'''
chsh() { [[ "$*" == '-s /usr/bin/zsh alice' ]]; }
configure_zsh
printf 'stale user additions\n' >> "$TARGET_HOME/.zshrc"
printf 'stale managed additions\n' >> "$TARGET_HOME/.config/setup-vps/zshrc"
configure_zsh
env -i HOME="$TARGET_HOME" PATH="$TARGET_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    TERM=xterm-256color SETUP_TEST_TRACE="$TEST_TRACE" SETUP_TEST_DEST="$TEST_DEST" \
    zsh -d -i -c '
        [[ ${+functions[z]} == 1 && ${+functions[fzf-history-widget]} == 1 && ${+functions[_zsh_highlight]} == 1 ]] || exit 10
        print -rl -- "cd_alias=$aliases[cd]" "cdi_alias=$aliases[cdi]"
        print -rl -- "ll_alias=$aliases[ll]" "la_alias=$aliases[la]" "lt_alias=$aliases[lt]"
        print -rl -- "history=$HISTFILE" "files=$FZF_DEFAULT_COMMAND" "directories=$FZF_ALT_C_COMMAND"
        cd -- "$SETUP_TEST_DEST"
        print -r -- "arrived=$PWD"
    '
''',
            env={"TEST_TRACE": trace, "TEST_DEST": destination},
        )
        self.assertEqual(
            trace.read_text().splitlines(),
            ["mise", "zoxide", "fzf", "starship", "fzf-tab", "autosuggestions", "highlighting"],
        )
        self.assertIn("cd_alias=z\n", result.stdout)
        self.assertIn("cdi_alias=zi\n", result.stdout)
        self.assertIn("ll_alias=eza --long --git", result.stdout)
        self.assertIn("la_alias=eza --long --all --git", result.stdout)
        self.assertIn("lt_alias=eza --tree --level=2", result.stdout)
        self.assertIn(f"history={self.home}/.zsh_history", result.stdout)
        self.assertIn("files=fd --type f --hidden --exclude .git", result.stdout)
        self.assertIn("directories=fd --type d --hidden --exclude .git", result.stdout)
        self.assertIn(f"arrived={destination}", result.stdout)
        self.assertNotIn("stale", (self.home / ".zshrc").read_text())
        self.assertNotIn("stale", (self.home / ".config/setup-vps/zshrc").read_text())


if __name__ == "__main__":
    unittest.main()
