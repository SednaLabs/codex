#![cfg(not(debug_assertions))]

use crate::contributor_slots::UpdatePromptContribution;
use crate::contributor_slots::contribute_update_prompt;
use crate::key_hint;
use crate::legacy_core::config::Config;
use crate::render::Insets;
use crate::render::renderable::ColumnRenderable;
use crate::render::renderable::Renderable;
use crate::render::renderable::RenderableExt as _;
use crate::selection_list::selection_option_row;
use crate::tui::FrameRequester;
use crate::tui::Tui;
use crate::tui::TuiEvent;
use crate::update_action::UpdateAction;
use crate::updates;
use crate::version::CODEX_DISPLAY_VERSION;
use crate::version::latest_release_notes_url;
use color_eyre::Result;
use crossterm::event::KeyCode;
use crossterm::event::KeyEvent;
use crossterm::event::KeyEventKind;
use crossterm::event::KeyModifiers;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::prelude::Widget;
use ratatui::style::Stylize as _;
use ratatui::text::Line;
use ratatui::widgets::Clear;
use ratatui::widgets::WidgetRef;
use tokio_stream::StreamExt;

pub(crate) enum UpdatePromptOutcome {
    Continue,
    RunUpdate(UpdateAction),
}

pub(crate) async fn run_update_prompt_if_needed(
    tui: &mut Tui,
    config: &Config,
) -> Result<UpdatePromptOutcome> {
    let Some(latest_version) = updates::get_upgrade_version_for_popup(config) else {
        return Ok(UpdatePromptOutcome::Continue);
    };
    let Some(update_action) = crate::update_action::get_update_action(config.sedna_release_channel)
    else {
        return Ok(UpdatePromptOutcome::Continue);
    };

    let mut screen =
        UpdatePromptScreen::new(tui.frame_requester(), latest_version.clone(), update_action);
    tui.draw(u16::MAX, |frame| {
        frame.render_widget_ref(&screen, frame.area());
    })?;

    let events = tui.event_stream();
    tokio::pin!(events);

    while !screen.is_done() {
        if let Some(event) = events.next().await {
            match event {
                TuiEvent::Key(key_event) => screen.handle_key(key_event),
                TuiEvent::Paste(_) => {}
                TuiEvent::Draw | TuiEvent::Resize => {
                    tui.draw(u16::MAX, |frame| {
                        frame.render_widget_ref(&screen, frame.area());
                    })?;
                }
            }
        } else {
            break;
        }
    }

    match screen.selection() {
        Some(UpdateSelection::UpdateNow) => {
            tui.terminal.clear()?;
            Ok(UpdatePromptOutcome::RunUpdate(update_action))
        }
        Some(UpdateSelection::NotNow) | None => Ok(UpdatePromptOutcome::Continue),
        Some(UpdateSelection::DontRemind) => {
            if let Err(err) = updates::dismiss_version(config, screen.latest_version()).await {
                tracing::error!("Failed to persist update dismissal: {err}");
            }
            Ok(UpdatePromptOutcome::Continue)
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum UpdateSelection {
    UpdateNow,
    NotNow,
    DontRemind,
}

struct UpdatePromptScreen {
    request_frame: FrameRequester,
    latest_version: String,
    copy: UpdatePromptContribution,
    highlighted: UpdateSelection,
    selection: Option<UpdateSelection>,
}

impl UpdatePromptScreen {
    fn new(
        request_frame: FrameRequester,
        latest_version: String,
        update_action: UpdateAction,
    ) -> Self {
        let update_command = update_action.command_str();
        let copy = contribute_update_prompt(UpdatePromptContribution::new(
            CODEX_DISPLAY_VERSION,
            latest_version.as_str(),
            update_command.as_str(),
        ));
        Self {
            request_frame,
            latest_version,
            copy,
            highlighted: UpdateSelection::UpdateNow,
            selection: None,
        }
    }

    fn handle_key(&mut self, key_event: KeyEvent) {
        if key_event.kind == KeyEventKind::Release {
            return;
        }
        if key_event.modifiers.contains(KeyModifiers::CONTROL)
            && matches!(key_event.code, KeyCode::Char('c') | KeyCode::Char('d'))
        {
            self.select(UpdateSelection::NotNow);
            return;
        }
        match key_event.code {
            KeyCode::Up | KeyCode::Char('k') => self.set_highlight(self.highlighted.prev()),
            KeyCode::Down | KeyCode::Char('j') => self.set_highlight(self.highlighted.next()),
            KeyCode::Char('1') => self.select(UpdateSelection::UpdateNow),
            KeyCode::Char('2') => self.select(UpdateSelection::NotNow),
            KeyCode::Char('3') => self.select(UpdateSelection::DontRemind),
            KeyCode::Enter => self.select(self.highlighted),
            KeyCode::Esc => self.select(UpdateSelection::NotNow),
            _ => {}
        }
    }

    fn set_highlight(&mut self, highlight: UpdateSelection) {
        if self.highlighted != highlight {
            self.highlighted = highlight;
            self.request_frame.schedule_frame();
        }
    }

    fn select(&mut self, selection: UpdateSelection) {
        self.highlighted = selection;
        self.selection = Some(selection);
        self.request_frame.schedule_frame();
    }

    fn is_done(&self) -> bool {
        self.selection.is_some()
    }

    fn selection(&self) -> Option<UpdateSelection> {
        self.selection
    }

    fn latest_version(&self) -> &str {
        self.latest_version.as_str()
    }
}

impl UpdateSelection {
    fn next(self) -> Self {
        match self {
            UpdateSelection::UpdateNow => UpdateSelection::NotNow,
            UpdateSelection::NotNow => UpdateSelection::DontRemind,
            UpdateSelection::DontRemind => UpdateSelection::UpdateNow,
        }
    }

    fn prev(self) -> Self {
        match self {
            UpdateSelection::UpdateNow => UpdateSelection::DontRemind,
            UpdateSelection::NotNow => UpdateSelection::UpdateNow,
            UpdateSelection::DontRemind => UpdateSelection::NotNow,
        }
    }
}

impl WidgetRef for &UpdatePromptScreen {
    fn render_ref(&self, area: Rect, buf: &mut Buffer) {
        Clear.render(area, buf);
        let release_notes_url = latest_release_notes_url();
        let mut column = ColumnRenderable::new();

        column.push("");
        column.push(Line::from(vec![
            "  ✨\u{200A}".bold().cyan(),
            self.copy.title.clone().bold(),
            " ".into(),
            self.copy.version_label.clone().dim(),
        ]));
        column.push("");
        column.push(
            Line::from(vec![
                self.copy.release_notes_label.clone().dim(),
                release_notes_url.as_str().dim().underlined(),
            ])
            .inset(Insets::tlbr(0, 2, 0, 0)),
        );
        column.push("");
        column.push(selection_option_row(
            0,
            self.copy.update_now_label.clone(),
            self.highlighted == UpdateSelection::UpdateNow,
        ));
        column.push(selection_option_row(
            1,
            self.copy.skip_label.clone(),
            self.highlighted == UpdateSelection::NotNow,
        ));
        column.push(selection_option_row(
            2,
            self.copy.skip_until_next_version_label.clone(),
            self.highlighted == UpdateSelection::DontRemind,
        ));
        column.push("");
        column.push(
            Line::from(vec![
                "Press ".dim(),
                key_hint::plain(KeyCode::Enter).into(),
                format!(" {}", self.copy.continue_hint).dim(),
            ])
            .inset(Insets::tlbr(0, 2, 0, 0)),
        );
        column.render(area, buf);
        crate::terminal_hyperlinks::mark_underlined_hyperlink(buf, area, &release_notes_url);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tui::FrameRequester;
    use crossterm::event::KeyCode;
    use crossterm::event::KeyEvent;
    use crossterm::event::KeyModifiers;

    fn new_prompt() -> UpdatePromptScreen {
        UpdatePromptScreen::new(
            FrameRequester::test_dummy(),
            "9.9.9".into(),
            UpdateAction::StandaloneUnix(codex_utils_version::SednaReleaseChannel::Stable),
        )
    }

    #[test]
    fn update_prompt_confirm_selects_update() {
        let mut screen = new_prompt();
        screen.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(screen.is_done());
        assert_eq!(screen.selection(), Some(UpdateSelection::UpdateNow));
    }

    #[test]
    fn update_prompt_dismiss_option_leaves_prompt_in_normal_state() {
        let mut screen = new_prompt();
        screen.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        screen.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(screen.is_done());
        assert_eq!(screen.selection(), Some(UpdateSelection::NotNow));
    }

    #[test]
    fn update_prompt_dont_remind_selects_dismissal() {
        let mut screen = new_prompt();
        screen.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        screen.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        screen.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(screen.is_done());
        assert_eq!(screen.selection(), Some(UpdateSelection::DontRemind));
    }

    #[test]
    fn update_prompt_ctrl_c_skips_update() {
        let mut screen = new_prompt();
        screen.handle_key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL));
        assert!(screen.is_done());
        assert_eq!(screen.selection(), Some(UpdateSelection::NotNow));
    }

    #[test]
    fn update_prompt_navigation_wraps_between_entries() {
        let mut screen = new_prompt();
        screen.handle_key(KeyEvent::new(KeyCode::Up, KeyModifiers::NONE));
        assert_eq!(screen.highlighted, UpdateSelection::DontRemind);
        screen.handle_key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
        assert_eq!(screen.highlighted, UpdateSelection::UpdateNow);
    }
}
