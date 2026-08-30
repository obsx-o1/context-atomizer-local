$d=(gp 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal').InstallLocation;
$u="$env:TEMP\Atomizer-Q-$([guid]::NewGuid()).exe";
try{
    [IO.File]::Copy("$d\Uninstall.exe",$u);
    $p=[Diagnostics.Process]::Start($u,"/S _?=$d");
    $p.WaitForExit();
    exit $p.ExitCode
}catch{
    exit 4
}finally{
    rm -LiteralPath $u -Force -EA 0
}
