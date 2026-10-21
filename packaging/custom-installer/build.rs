use std::{env, fs, path::PathBuf};

fn main() {
    println!("cargo:rerun-if-changed=app.json");
    println!("cargo:rerun-if-env-changed=XYN_INSTALLER_BUNDLE");
    let app: serde_json::Value =
        serde_json::from_str(&fs::read_to_string("app.json").unwrap()).unwrap();
    let bundle = PathBuf::from(
        env::var_os("XYN_INSTALLER_BUNDLE")
            .expect("Run build-custom.ps1 to prepare the embedded bundle"),
    );
    for name in ["xyn-payload.exe", "ui.html", "payload.json", "version.txt"] {
        let source = bundle.join(name);
        assert!(source.is_file(), "Missing {}", source.display());
        println!("cargo:rerun-if-changed={}", source.display());
        fs::copy(
            source,
            PathBuf::from(env::var_os("OUT_DIR").unwrap()).join(name),
        )
        .unwrap();
    }
    let version = fs::read_to_string(bundle.join("version.txt")).unwrap();
    let mut parts: Vec<u64> = version
        .trim()
        .split('.')
        .map(|p| p.parse().unwrap())
        .collect();
    parts.resize(4, 0);
    let file_version = (parts[0] << 48) | (parts[1] << 32) | (parts[2] << 16) | parts[3];
    let name = app["name"].as_str().unwrap();
    let mut res = winresource::WindowsResource::new();
    res.set_icon(app["icon"].as_str().unwrap())
        .set("ProductName", name)
        .set("FileDescription", &format!("{name} Setup"))
        .set("FileVersion", version.trim())
        .set("ProductVersion", version.trim())
        .set_version_info(winresource::VersionInfo::FILEVERSION, file_version)
        .set_version_info(winresource::VersionInfo::PRODUCTVERSION, file_version)
        .set_manifest(r#"<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"><assemblyIdentity version="1.0.0.0" name="Xyn.Installer" type="win32"/><trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo><compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application><supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/></application></compatibility></assembly>"#)
        .compile().unwrap();
}
