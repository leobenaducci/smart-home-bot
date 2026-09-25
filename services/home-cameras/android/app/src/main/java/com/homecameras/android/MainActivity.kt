package com.homecameras.android

import android.annotation.SuppressLint
import android.os.Bundle
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val webView = findViewById<WebView>(R.id.webview)

        // Enable JavaScript (if needed for the web content)
        webView.settings.javaScriptEnabled = true

        // Enable zoom controls
        webView.settings.setSupportZoom(true)
        webView.settings.builtInZoomControls = true
        webView.settings.displayZoomControls = false

        // Set WebViewClient to handle URL loading
        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView?,
                request: WebResourceRequest?
            ): Boolean {
                // Only allow loading the specified URL
                val url = request?.url.toString()
                return if (url == "http://compute.home:5000" || url == "https://ubuntu.tail7ca5f3.ts.net") {
                    false // Allow loading
                } else {
                    true // Block loading
                }
            }

            // For older Android versions
            override fun shouldOverrideUrlLoading(view: WebView?, url: String?): Boolean {
                return if (url == "http://compute.home:5000" || url == "https://ubuntu.tail7ca5f3.ts.net") {
                    false // Allow loading
                } else {
                    true // Block loading
                }
            }
            
            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                
                // If error occurred on main URL and fallback hasn't loaded yet
                if (request?.url.toString() == "http://compute.home:5000") {
                    webView.loadUrl("https://ubuntu.tail7ca5f3.ts.net")
                }
            }
            
            // For older Android versions (deprecated method)
            override fun onReceivedError(view: WebView?, url: String?, error: String?) {
                super.onReceivedError(view, url, error)
                
                // If error occurred on main URL and fallback hasn't loaded yet
                if (url == "http://compute.home:5000") {
                    webView.loadUrl("https://ubuntu.tail7ca5f3.ts.net")
                }
            }
        }

        // Load the specified URL
        webView.loadUrl("http://compute.home:5000")
    }

    // Handle back button to prevent navigation
    override fun onBackPressed() {
        // Do nothing - prevent back navigation
    }
}