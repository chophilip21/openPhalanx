//! Terminal presentation: colors, banner, spinners and panels.
//!
//! Everything degrades to plain text when the stream isn't a terminal or
//! `NO_COLOR` is set, so output piped into Aider (`/run oppx search`) or a
//! script stays clean.

use std::time::Duration;

use console::{measure_text_width, style, Term};
use indicatif::{ProgressBar, ProgressStyle};

/// Large block letters (5 rows), one glyph per letter of OPENPHALANX.
const LARGE: &[(char, [&str; 5])] = &[
    ('O', [" ██████  ", "██    ██ ", "██    ██ ", "██    ██ ", " ██████  "]),
    ('P', ["██████  ", "██   ██ ", "██████  ", "██      ", "██      "]),
    ('E', ["███████ ", "██      ", "█████   ", "██      ", "███████ "]),
    ('N', ["███    ██ ", "████   ██ ", "██ ██  ██ ", "██  ██ ██ ", "██   ████ "]),
    ('H', ["██   ██ ", "██   ██ ", "███████ ", "██   ██ ", "██   ██ "]),
    ('A', [" █████  ", "██   ██ ", "███████ ", "██   ██ ", "██   ██ "]),
    ('L', ["██      ", "██      ", "██      ", "██      ", "███████ "]),
    ('X', ["██   ██ ", " ██ ██  ", "  ███   ", " ██ ██  ", "██   ██ "]),
];
/// Compact half-block letters (2 rows) for narrow terminals.
const SMALL: &[(char, [&str; 2])] = &[
    ('O', ["█▀█ ", "█▄█ "]),
    ('P', ["█▀█ ", "█▀▀ "]),
    ('E', ["█▀▀ ", "██▄ "]),
    ('N', ["█▄ █ ", "█ ▀█ "]),
    ('H', ["█ █ ", "█▀█ "]),
    ('A', ["▄▀█ ", "█▀█ "]),
    ('L', ["█   ", "█▄▄ "]),
    ('X', ["▀▄▀ ", "█ █ "]),
];
const WORD: &str = "OPENPHALANX";
/// Green to cyan to blue (ANSI 256 colors), echoing the app's accent.
const GRADIENT: [u8; 6] = [48, 49, 43, 44, 38, 33];

fn render<const N: usize>(font: &[(char, [&str; N])]) -> Vec<String> {
    let mut rows = vec![String::new(); N];
    for c in WORD.chars() {
        let glyph = &font.iter().find(|(g, _)| *g == c).expect("glyph for every letter").1;
        for (row, part) in rows.iter_mut().zip(glyph) {
            row.push_str(part);
        }
    }
    rows.iter().map(|r| r.trim_end().to_string()).collect()
}

/// Colors each column along the gradient, left to right.
fn gradient(row: &str, width: usize) -> String {
    row.chars()
        .enumerate()
        .map(|(i, ch)| {
            if ch == ' ' {
                " ".to_string()
            } else {
                let color = GRADIENT[(i * GRADIENT.len() / width.max(1)).min(GRADIENT.len() - 1)];
                style(ch).color256(color).bold().to_string()
            }
        })
        .collect()
}

/// Call once at startup: honors `NO_COLOR` for both streams.
pub fn init() {
    if std::env::var_os("NO_COLOR").is_some_and(|v| !v.is_empty()) {
        console::set_colors_enabled(false);
        console::set_colors_enabled_stderr(false);
    }
}

fn stdout_fancy() -> bool {
    Term::stdout().is_term() && console::colors_enabled()
}

fn stderr_fancy() -> bool {
    Term::stderr().is_term() && console::colors_enabled_stderr()
}

/// The OPENPHALANX banner with a gradient and tagline. Large letters when
/// the terminal is wide enough, compact ones otherwise; one plain line when piped.
pub fn banner() {
    if !stdout_fancy() {
        println!("OpenPhalanx client (oppx {})", env!("CARGO_PKG_VERSION"));
        return;
    }
    let large = render(LARGE);
    let width = large.iter().map(|r| measure_text_width(r)).max().unwrap_or(0);
    let cols = Term::stdout().size().1 as usize;
    let rows = if cols >= width + 2 { large } else { render(SMALL) };
    let width = rows.iter().map(|r| measure_text_width(r)).max().unwrap_or(0);
    println!();
    for row in &rows {
        println!("  {}", gradient(row, width));
    }
    println!(
        "  {} {}",
        style(format!("oppx v{}", env!("CARGO_PKG_VERSION"))).bold(),
        style("· run a GPU server's coding model from any laptop").dim()
    );
    println!();
}

/// A spinner on stderr; invisible when stderr isn't a terminal.
pub struct Spinner(ProgressBar);

pub fn spinner(message: impl Into<String>) -> Spinner {
    let message = message.into();
    if !stderr_fancy() {
        return Spinner(ProgressBar::hidden());
    }
    let pb = ProgressBar::new_spinner();
    pb.set_style(
        ProgressStyle::with_template("  {spinner:.cyan} {msg}")
            .expect("valid template")
            .tick_strings(&["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏", "✓"]),
    );
    pb.set_message(message);
    pb.enable_steady_tick(Duration::from_millis(80));
    Spinner(pb)
}

impl Spinner {
    /// Replaces the spinner with a green check line.
    pub fn done(self, message: impl AsRef<str>) {
        if self.0.is_hidden() {
            // Not a terminal: still report the step, as plain text.
            eprintln!("  ✓ {}", message.as_ref());
            return;
        }
        self.0.finish_and_clear();
        eprintln!("  {} {}", style("✓").green().bold().for_stderr(), message.as_ref());
    }

    /// Removes the spinner without a message (e.g. before an error).
    pub fn clear(self) {
        self.0.finish_and_clear();
    }
}

/// `✓ label  value` (green), for panels and step output.
pub fn ok(label: &str, value: impl AsRef<str>) -> String {
    format!("{} {:<12} {}", style("✓").green().bold(), style(label).bold(), value.as_ref())
}

/// `! label  value` (yellow).
pub fn warn(label: &str, value: impl AsRef<str>) -> String {
    format!("{} {:<12} {}", style("!").yellow().bold(), style(label).bold(), value.as_ref())
}

/// `✗ label  value` (red).
pub fn bad(label: &str, value: impl AsRef<str>) -> String {
    format!("{} {:<12} {}", style("✗").red().bold(), style(label).bold(), value.as_ref())
}

pub fn dim(s: impl std::fmt::Display) -> String {
    style(s).dim().to_string()
}

pub fn accent(s: impl std::fmt::Display) -> String {
    style(s).cyan().bold().to_string()
}

/// A rounded box with a title; plain indented lines when piped.
pub fn panel(title: &str, rows: &[String]) {
    if !stdout_fancy() {
        println!("{title}");
        for r in rows {
            println!("  {}", console::strip_ansi_codes(r));
        }
        return;
    }
    // Every line is `content + 4` wide: "│ " + content + " │".
    let content = rows
        .iter()
        .map(|r| measure_text_width(r))
        .chain([measure_text_width(title) + 3])
        .max()
        .unwrap_or(0);
    let border = |s: &str| style(s).color256(38).to_string();
    let title_w = measure_text_width(title) + 2; // " title "
    println!(
        "  {}{}{}",
        border("╭─"),
        style(format!(" {title} ")).bold(),
        border(&format!("{}╮", "─".repeat(content + 4 - 2 - title_w - 1)))
    );
    for r in rows {
        let pad = content - measure_text_width(r);
        println!("  {} {}{} {}", border("│"), r, " ".repeat(pad), border("│"));
    }
    println!("  {}", border(&format!("╰{}╯", "─".repeat(content + 2))));
}

/// One indented line (aligns with panels and spinners).
pub fn line(s: impl std::fmt::Display) {
    println!("  {s}");
}

/// Red `error:` line on stderr.
pub fn error(message: impl std::fmt::Display) {
    eprintln!("{} {message}", style("error:").red().bold().for_stderr());
}

/// Yellow `warning:` line on stderr.
pub fn warning(message: impl std::fmt::Display) {
    eprintln!("{} {message}", style("warning:").yellow().bold().for_stderr());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn banner_spells_openphalanx_in_both_sizes() {
        let large = render(LARGE);
        let small = render(SMALL);
        assert_eq!((large.len(), small.len()), (5, 2));
        let large_w = large.iter().map(|r| measure_text_width(r)).max().unwrap();
        let small_w = small.iter().map(|r| measure_text_width(r)).max().unwrap();
        assert!(large_w > 80 && small_w < 60, "large {large_w}, small {small_w}");
        // Every glyph row of a letter has the same width, so letters line up.
        for (_, g) in LARGE {
            assert!(g.iter().all(|r| measure_text_width(r) == measure_text_width(g[0])));
        }
        for (_, g) in SMALL {
            assert!(g.iter().all(|r| measure_text_width(r) == measure_text_width(g[0])));
        }
    }

    #[test]
    fn status_marks_carry_their_text() {
        console::set_colors_enabled(false);
        assert_eq!(ok("device", "laptop").trim_end(), "✓ device       laptop");
        assert!(bad("model", "down").starts_with("✗ model"));
        assert!(warn("model", "loading").starts_with("! model"));
    }
}
