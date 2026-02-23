# Google OAuth Setup Guide

## Issue: Error 403 - access_denied

Your OAuth app is in **Testing** mode and restricted to approved test users.

## Solution Options

### Option 1: Add Your Email as a Test User (Quick Fix)

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Select your project: **agentic-471801**
3. Navigate to: **APIs & Services** → **OAuth consent screen**
4. Scroll down to **Test users** section
5. Click **+ ADD USERS**
6. Add your Google account email address
7. Click **SAVE**
8. Try the authentication flow again

### Option 2: Publish the App (For Production Use)

If you want anyone to be able to use your app:

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Select your project: **agentic-471801**
3. Navigate to: **APIs & Services** → **OAuth consent screen**
4. Click **PUBLISH APP** button
5. Confirm the publication

⚠️ **Note:** Publishing requires:
- Complete app information (name, logo, privacy policy, etc.)
- May require Google verification for sensitive scopes
- Can take time for approval

### Option 3: Use Internal User Type (If G Suite/Workspace)

If you have a Google Workspace account:

1. Change the user type to **Internal**
2. This restricts access to users in your organization
3. No verification needed

## Current App Information

- **Project ID:** agentic-471801
- **Client ID:** 686393938713-n647q5rb9d1480a6e2jkptvg8u2s7agq.apps.googleusercontent.com
- **Status:** Testing (restricted access)

## Recommended: Option 1

For testing purposes, **adding your email as a test user** is the quickest solution.

## Direct Link

[OAuth Consent Screen Settings](https://console.cloud.google.com/apis/credentials/consent?project=agentic-471801)
