use crate::JsonSchema;
use crate::ResponsesApiTool;
use codex_protocol::dynamic_tools::DynamicToolSpec;
use serde_json::json;
use std::collections::BTreeMap;

pub const BROWSER_OBSERVE_TOOL_NAME: &str = "browser_observe";
pub const BROWSER_STEP_TOOL_NAME: &str = "browser_step";

const OBSERVE_SCOPE_VIEWPORT: &str = "viewport";
const OBSERVE_SCOPE_VIEWPORT_AND_PAGE: &str = "viewport_and_page";
const BACKEND_AUTO: &str = "auto";
const BACKEND_BROWSER: &str = "browser";
const BACKEND_CHROMIUM: &str = "chromium";
const BACKEND_IAB: &str = "iab";
const BACKEND_CHROME: &str = "chrome";
const CAPTURE_SCROLL_CURRENT: &str = "current";
const CAPTURE_SCROLL_TOP: &str = "top";
const CAPTURE_SCROLL_BOTTOM: &str = "bottom";

const STEP_ACTIONS: [&str; 17] = [
    "navigate",
    "click",
    "type",
    "focus",
    "clear",
    "keypress",
    "key_down",
    "key_up",
    "scroll",
    "mouse_wheel",
    "wait",
    "select",
    "drag",
    "hover",
    "mouse_move",
    "mouse_down",
    "mouse_up",
];

pub fn canonical_browser_dynamic_tool(tool: &DynamicToolSpec) -> Option<ResponsesApiTool> {
    if tool.namespace.is_some() {
        return None;
    }

    match tool.name.as_str() {
        BROWSER_OBSERVE_TOOL_NAME => Some(create_browser_observe_tool(tool.defer_loading)),
        BROWSER_STEP_TOOL_NAME => Some(create_browser_step_tool(tool.defer_loading)),
        _ => None,
    }
}

fn create_browser_observe_tool(defer_loading: bool) -> ResponsesApiTool {
    let properties = BTreeMap::from([
        ("backend".to_string(), backend_schema()),
        (
            "prompt".to_string(),
            JsonSchema::string(Some(
                "Optional observation focus hint describing what the model should inspect."
                    .to_string(),
            )),
        ),
        (
            "scope".to_string(),
            string_enum(
                &[OBSERVE_SCOPE_VIEWPORT, OBSERVE_SCOPE_VIEWPORT_AND_PAGE],
                "Whether to capture only the browser viewport or pair it with compact page metadata.",
            ),
        ),
        (
            "view".to_string(),
            view_schema(Some(
                "Optional viewport metadata or requested browser viewport size.".to_string(),
            )),
        ),
        (
            "captures".to_string(),
            JsonSchema::array(
                capture_schema(),
                Some(
                    "Optional labeled capture bundle, capped by the provider. Use for desktop/mobile or top/bottom UX review."
                        .to_string(),
                ),
            ),
        ),
        (
            "save_artifact".to_string(),
            JsonSchema::boolean(Some(
                "Request that the provider save screenshot and manifest artifacts for audit."
                    .to_string(),
            )),
        ),
        (
            "artifact_label".to_string(),
            JsonSchema::string(Some("Short label for saved browser artifacts.".to_string())),
        ),
        (
            "service_profile".to_string(),
            JsonSchema::string(Some(
                "Named local service-account browser profile for allowed-host navigation."
                    .to_string(),
            )),
        ),
        (
            "extra_http_headers".to_string(),
            string_map_schema(Some(
                "Optional direct navigation headers; providers should require explicit local opt-in and redaction."
                    .to_string(),
            )),
        ),
        (
            "extra_http_headers_env".to_string(),
            string_map_schema(Some(
                "Header names mapped to environment variables for direct navigation headers."
                    .to_string(),
            )),
        ),
        (
            "allowed_hosts".to_string(),
            JsonSchema::array(
                JsonSchema::string(Some("Host allowed to receive service headers.".to_string())),
                Some("Allowed hosts for service-account or per-call headers.".to_string()),
            ),
        ),
        ("inspection".to_string(), inspection_schema()),
        ("interaction_map".to_string(), interaction_map_schema()),
    ]);

    ResponsesApiTool {
        name: BROWSER_OBSERVE_TOOL_NAME.to_string(),
        description:
            "Capture the current browser viewport as a model-visible screenshot, optionally with compact page metadata.".to_string(),
        strict: false,
        defer_loading: defer_loading.then_some(true),
        parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
        output_schema: None,
    }
}

fn create_browser_step_tool(defer_loading: bool) -> ResponsesApiTool {
    let action_item_properties = step_action_properties(/*include_type*/ true);
    let mut properties = step_action_properties(/*include_type*/ false);
    properties.insert("backend".to_string(), backend_schema());
    properties.insert(
        "action".to_string(),
        string_enum(
            &STEP_ACTIONS,
            "Single-action compatibility field. Prefer actions[] for new calls.",
        ),
    );
    properties.insert(
        "actions".to_string(),
        JsonSchema::array(
            JsonSchema::object(
                action_item_properties,
                Some(vec!["type".to_string()]),
                Some(false.into()),
            ),
            Some(
                "Preferred batched browser action list. Execute actions in order before observing again.".to_string(),
            ),
        ),
    );
    properties.insert(
        "post_observe_scope".to_string(),
        string_enum(
            &[OBSERVE_SCOPE_VIEWPORT, OBSERVE_SCOPE_VIEWPORT_AND_PAGE],
            "Whether the post-action observation should include compact page metadata.",
        ),
    );
    properties.insert(
        "settle_ms".to_string(),
        JsonSchema::integer(Some(
            "Optional post-action wait before the final observation.".to_string(),
        )),
    );
    properties.insert(
        "save_artifact".to_string(),
        JsonSchema::boolean(Some(
            "Request that the provider save screenshot and manifest artifacts for audit."
                .to_string(),
        )),
    );
    properties.insert(
        "artifact_label".to_string(),
        JsonSchema::string(Some("Short label for saved browser artifacts.".to_string())),
    );
    properties.insert(
        "service_profile".to_string(),
        JsonSchema::string(Some(
            "Named local service-account browser profile for allowed-host navigation.".to_string(),
        )),
    );
    properties.insert(
        "extra_http_headers".to_string(),
        string_map_schema(Some(
            "Optional direct navigation headers; providers should require explicit local opt-in and redaction."
                .to_string(),
        )),
    );
    properties.insert(
        "extra_http_headers_env".to_string(),
        string_map_schema(Some(
            "Header names mapped to environment variables for direct navigation headers."
                .to_string(),
        )),
    );
    properties.insert(
        "allowed_hosts".to_string(),
        JsonSchema::array(
            JsonSchema::string(Some("Host allowed to receive service headers.".to_string())),
            Some("Allowed hosts for service-account or per-call headers.".to_string()),
        ),
    );
    properties.insert(
        "view".to_string(),
        view_schema(Some(
            "Optional requested browser viewport size or scroll position for the post-action observation."
                .to_string(),
        )),
    );
    properties.insert("inspection".to_string(), inspection_schema());
    properties.insert("interaction_map".to_string(), interaction_map_schema());

    ResponsesApiTool {
        name: BROWSER_STEP_TOOL_NAME.to_string(),
        description:
            "Perform one or more bounded browser actions, then return a fresh post-action viewport screenshot, summary, and current view metadata.".to_string(),
        strict: false,
        defer_loading: defer_loading.then_some(true),
        parameters: JsonSchema::object(properties, /*required*/ None, Some(false.into())),
        output_schema: None,
    }
}

fn step_action_properties(include_type: bool) -> BTreeMap<String, JsonSchema> {
    let mut properties = BTreeMap::from([
        (
            "url".to_string(),
            JsonSchema::string(Some("URL for navigate actions.".to_string())),
        ),
        (
            "selector".to_string(),
            selector_schema(Some(
                "Optional selector for element-backed interactions. Use a string CSS selector or an object with css, text, label, role/name, placeholder, test_id, title, alt_text, exact, and strict fields.".to_string(),
            )),
        ),
        (
            "text".to_string(),
            JsonSchema::string(Some(
                "Text to type into the focused field or target element.".to_string(),
            )),
        ),
        (
            "x".to_string(),
            JsonSchema::number(Some(
                "X coordinate in the current browser viewport.".to_string(),
            )),
        ),
        (
            "y".to_string(),
            JsonSchema::number(Some(
                "Y coordinate in the current browser viewport.".to_string(),
            )),
        ),
        (
            "x1".to_string(),
            JsonSchema::number(Some("Start X coordinate for drag actions.".to_string())),
        ),
        (
            "y1".to_string(),
            JsonSchema::number(Some("Start Y coordinate for drag actions.".to_string())),
        ),
        (
            "x2".to_string(),
            JsonSchema::number(Some("End X coordinate for drag actions.".to_string())),
        ),
        (
            "y2".to_string(),
            JsonSchema::number(Some("End Y coordinate for drag actions.".to_string())),
        ),
        (
            "scroll_x".to_string(),
            JsonSchema::number(Some(
                "Horizontal scroll delta for scroll actions.".to_string(),
            )),
        ),
        (
            "scroll_y".to_string(),
            JsonSchema::number(Some(
                "Vertical scroll delta for scroll actions.".to_string(),
            )),
        ),
        (
            "button".to_string(),
            string_enum(
                &["left", "right", "middle"],
                "Mouse button for click, mouse_down, mouse_up, or drag actions.",
            ),
        ),
        (
            "click_count".to_string(),
            JsonSchema::integer(Some(
                "Number of clicks for click actions; use 2 for double-click.".to_string(),
            )),
        ),
        (
            "delay_ms".to_string(),
            JsonSchema::integer(Some(
                "Human-paced delay in milliseconds for typing, keypress, click, or mouse events."
                    .to_string(),
            )),
        ),
        (
            "steps".to_string(),
            JsonSchema::integer(Some(
                "Intermediate mouse movement steps for human-like move, click, drag, hover, mouse_down, or mouse_up actions.".to_string(),
            )),
        ),
        (
            "modifiers".to_string(),
            JsonSchema::array(
                string_enum(
                    &["Alt", "Control", "Meta", "Shift"],
                    "Keyboard modifier to hold while performing the action.",
                ),
                Some("Keyboard modifiers to hold while performing a mouse action.".to_string()),
            ),
        ),
        (
            "key".to_string(),
            JsonSchema::string(Some(
                "Key, key combination, or key name for keypress, key_down, or key_up actions."
                    .to_string(),
            )),
        ),
        (
            "keys".to_string(),
            JsonSchema::array(
                JsonSchema::string(Some("One key in a key combination.".to_string())),
                Some("Key combination to send.".to_string()),
            ),
        ),
        (
            "ms".to_string(),
            JsonSchema::integer(Some("Milliseconds to wait.".to_string())),
        ),
        (
            "method".to_string(),
            string_enum(
                &["keyboard", "fill"],
                "Text entry method. keyboard sends human-like key events; fill uses DOM-level Playwright fill as a compatibility escape hatch.",
            ),
        ),
        (
            "replace".to_string(),
            JsonSchema::boolean(Some(
                "For keyboard text entry with a selector, select existing text and replace it before typing."
                    .to_string(),
            )),
        ),
        (
            "timeout_secs".to_string(),
            JsonSchema::integer(Some(
                "Optional provider-side timeout in seconds for the action and observation."
                    .to_string(),
            )),
        ),
        (
            "tab_id".to_string(),
            JsonSchema::string(Some(
                "Optional opaque browser tab identifier supplied by the provider.".to_string(),
            )),
        ),
    ]);

    if include_type {
        properties.insert(
            "type".to_string(),
            string_enum(
                &STEP_ACTIONS,
                "Browser action to execute before the post-action observation.",
            ),
        );
    }

    properties
}

fn capture_schema() -> JsonSchema {
    JsonSchema::object(
        BTreeMap::from([
            (
                "label".to_string(),
                JsonSchema::string(Some("Short label for this capture.".to_string())),
            ),
            (
                "viewportWidth".to_string(),
                JsonSchema::number(Some("Capture viewport width in pixels.".to_string())),
            ),
            (
                "viewportHeight".to_string(),
                JsonSchema::number(Some("Capture viewport height in pixels.".to_string())),
            ),
            (
                "scroll".to_string(),
                string_enum(
                    &[
                        CAPTURE_SCROLL_CURRENT,
                        CAPTURE_SCROLL_TOP,
                        CAPTURE_SCROLL_BOTTOM,
                    ],
                    "Where to scroll before capture.",
                ),
            ),
            (
                "scrollY".to_string(),
                JsonSchema::number(Some("Explicit vertical scroll position.".to_string())),
            ),
            (
                "settle_ms".to_string(),
                JsonSchema::number(Some(
                    "Milliseconds to wait after applying this capture view.".to_string(),
                )),
            ),
        ]),
        /*required*/ None,
        Some(false.into()),
    )
}

fn view_schema(description: Option<String>) -> JsonSchema {
    let mut schema = JsonSchema::object(
        BTreeMap::from([
            (
                "viewportWidth".to_string(),
                JsonSchema::number(Some("Browser viewport width in pixels.".to_string())),
            ),
            (
                "viewportHeight".to_string(),
                JsonSchema::number(Some("Browser viewport height in pixels.".to_string())),
            ),
            (
                "scrollY".to_string(),
                JsonSchema::number(Some(
                    "Browser vertical scroll position in pixels.".to_string(),
                )),
            ),
        ]),
        /*required*/ None,
        Some(false.into()),
    );
    schema.description = description;
    schema
}

fn backend_schema() -> JsonSchema {
    string_enum(
        &[
            BACKEND_AUTO,
            BACKEND_BROWSER,
            BACKEND_CHROME,
            BACKEND_CHROMIUM,
            BACKEND_IAB,
        ],
        "Preferred browser provider backend. Use auto unless the task requires a specific browser route such as Chrome, Chromium, or the in-app browser.",
    )
}

fn inspection_schema() -> JsonSchema {
    JsonSchema::object(
        BTreeMap::from([(
            "sections".to_string(),
            JsonSchema::array(
                string_enum(
                    &["runtime", "frames", "console", "network"],
                    "Bounded read-only diagnostic sections to include.",
                ),
                Some("Optional fixed diagnostic sections; omitted means all sections.".to_string()),
            ),
        ]),
        /*required*/ None,
        Some(false.into()),
    )
}

fn interaction_map_schema() -> JsonSchema {
    JsonSchema::object(
        BTreeMap::from([
            (
                "scope".to_string(),
                string_enum(
                    &["viewport", "page"],
                    "Interaction-map scope; viewport is the default.",
                ),
            ),
            (
                "offset".to_string(),
                JsonSchema::integer(Some(
                    "Optional nonnegative page-result offset, bounded by the provider.".to_string(),
                )),
            ),
        ]),
        /*required*/ None,
        Some(false.into()),
    )
}

fn permissive_object(description: Option<String>) -> JsonSchema {
    let mut schema = JsonSchema::object(BTreeMap::new(), /*required*/ None, Some(true.into()));
    schema.description = description;
    schema
}

fn string_map_schema(description: Option<String>) -> JsonSchema {
    let mut schema = JsonSchema::object(
        BTreeMap::new(),
        /*required*/ None,
        Some(JsonSchema::string(/*description*/ None).into()),
    );
    schema.description = description;
    schema
}

fn selector_schema(description: Option<String>) -> JsonSchema {
    JsonSchema::any_of(
        vec![
            JsonSchema::string(Some("CSS selector string.".to_string())),
            permissive_object(Some(
                "Selector object for accessibility-oriented or CSS selector lookup.".to_string(),
            )),
        ],
        description,
    )
}

fn string_enum(values: &[&str], description: &str) -> JsonSchema {
    JsonSchema::string_enum(
        values.iter().map(|value| json!(*value)).collect::<Vec<_>>(),
        Some(description.to_string()),
    )
}

#[cfg(test)]
#[path = "browser_tool_tests.rs"]
mod tests;
