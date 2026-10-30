package com.example.cloudinfraservicedesk;

import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.WebView;

import androidx.activity.OnBackPressedCallback;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        // The ALB OIDC session lives in cookies set by portal-api.example.com
        // (a different origin than the portal page), so make sure the WebView keeps them.
        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(getBridge().getWebView(), true);

        // Back button: walk back through WebView history, exit the app at the root.
        // (Capacitor's built-in handler is disabled in capacitor.config.json because it
        // does nothing at the root when the page registers no backButton listener.)
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                WebView webView = getBridge().getWebView();
                if (webView != null && webView.canGoBack()) {
                    webView.goBack();
                } else {
                    finish();
                }
            }
        });
    }

    @Override
    public void onPause() {
        super.onPause();
        // Persist session cookies so the login survives the app being killed.
        CookieManager.getInstance().flush();
    }
}
