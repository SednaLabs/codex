struct Response {
    success: bool,
    content_items: Vec<String>,
    error: Option<String>,
}

type ProviderResult = Result<Response, String>;

fn build_response() -> Response {
    Response {
        success: true,
        content_items: vec!["native-image".to_string()],
        error: None,
    }
}

fn require_native_image_for_visual_response(response: &mut Response, message: &str) {
    if response.content_items.is_empty() {
        response.success = false;
        response.error = Some(message.to_string());
    }
}

mod held_out_safe_browser {
    use super::*;

    pub fn handle_with_provider() -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        Ok(response)
    }
}

mod held_out_bad_browser {
    use super::*;

    pub fn handle_with_provider() -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        response.content_items = Vec::new();
        Ok(response)
    }
}
