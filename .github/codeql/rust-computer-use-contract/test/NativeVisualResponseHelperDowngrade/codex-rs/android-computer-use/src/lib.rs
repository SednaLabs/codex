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

// Mutation: the helper name remains the same but no longer downgrades a
// missing-image response.  Call-site name matching alone must not trust it.
fn require_native_image_for_visual_response(_response: &mut Response, _message: &str) {}

fn observe() -> ProviderResult {
    let mut response = build_response();
    require_native_image_for_visual_response(&mut response, "missing image");
    Ok(response)
}
