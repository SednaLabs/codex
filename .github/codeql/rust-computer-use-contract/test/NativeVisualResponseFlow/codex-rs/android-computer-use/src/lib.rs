struct Response {
    success: bool,
    content_items: Vec<String>,
    note: String,
    error: Option<String>,
}

type ProviderResult = Result<Response, String>;

fn build_response() -> Response {
    Response {
        success: true,
        content_items: vec!["native-image".to_string()],
        note: "metadata".to_string(),
        error: None,
    }
}

fn action_failure_response() -> Response {
    Response {
        success: false,
        content_items: Vec::new(),
        note: "failed".to_string(),
        error: Some("action failed".to_string()),
    }
}

fn require_native_image_for_visual_response(response: &mut Response, message: &str) {
    if response.content_items.is_empty() {
        response.success = false;
        response.error = Some(message.to_string());
    }
}

fn wrap_response(response: Response) -> Response {
    response
}

mod missing_guard {
    use super::*;

    pub fn observe() -> ProviderResult {
        Ok(build_response())
    }
}

mod wrong_guarded_object {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut guarded = build_response();
        let returned = build_response();
        require_native_image_for_visual_response(&mut guarded, "missing image");
        Ok(returned)
    }
}

mod branch_only_guard {
    use super::*;

    pub fn observe(enabled: bool) -> ProviderResult {
        let mut response = build_response();
        if enabled {
            require_native_image_for_visual_response(&mut response, "missing image");
        }
        Ok(response)
    }
}

mod overwritten_response {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        response = build_response();
        Ok(response)
    }
}

mod success_reset {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        response.success = true;
        Ok(response)
    }
}

mod image_removed {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        response.content_items = Vec::new();
        Ok(response)
    }
}

mod alias_field {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = build_response();
        let mut image_field = response.content_items;
        require_native_image_for_visual_response(&mut image_field, "wrong object");
        Ok(response)
    }
}

mod safe_alias {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = build_response();
        let alias = &mut response;
        require_native_image_for_visual_response(alias, "missing image");
        Ok(response)
    }
}

mod safe_wrapper_return {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = wrap_response(build_response());
        require_native_image_for_visual_response(&mut response, "missing image");
        Ok(response)
    }
}

mod safe_metadata_only {
    use super::*;

    pub fn observe() -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        response.note = "updated metadata".to_string();
        Ok(response)
    }
}

mod safe_exhaustive_match {
    use super::*;

    pub fn observe(enabled: bool) -> ProviderResult {
        let mut response = build_response();
        require_native_image_for_visual_response(&mut response, "missing image");
        match enabled {
            true => Ok(response),
            false => Ok(action_failure_response()),
        }
    }
}

mod safe_definite_failure {
    use super::*;

    pub fn step() -> ProviderResult {
        Ok(action_failure_response())
    }
}

mod safe_borrow_reborrow {
    use super::*;

    pub fn install_build_from_run() -> ProviderResult {
        let mut response = build_response();
        let borrowed = &mut response;
        require_native_image_for_visual_response(borrowed, "missing image");
        Ok(response)
    }
}
