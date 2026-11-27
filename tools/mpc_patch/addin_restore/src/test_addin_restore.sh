#!/bin/sh
# Host tests of addin_restore.c (build.sh test runs this in Docker under ASan/UBSan; plain `sh test_addin_restore.sh` with gcc works too).
# Each case builds a fake device under a temp root: /data/mpc-addins, MPC's environment, /run, and a stand-in for the 16-pad
# patch's saved script (its real `reapply` is covered by ../../test_script_gen2.sh).
set -eu
cd "$(dirname "$0")"
CFLAGS=${CFLAGS:--O1 -g -Wall -Wextra -Werror}
gcc $CFLAGS -DAR_TEST -fPIC -shared -std=gnu11 addin_restore.c -o /tmp/addin_restore_test.so
gcc $CFLAGS -std=gnu11 test_driver.c -ldl -o /tmp/ar_driver
fails=0; n=0
fresh() { R=$(mktemp -d); mkdir -p "$R/data" "$R/run" "$R/tmp"; : > "$R/environ"; }
addin() { mkdir -p "$R/data/mpc-addins/$1"; : > "$R/data/mpc-addins/$1/$2"; }
env_preload() { printf 'HOME=/root\0LD_PRELOAD=%s\0PATH=/bin\0' "$1" > "$R/environ"; }
drumpad() {   # $1: fits (reapply writes the drop-in) or refuses
    mkdir -p "$R/data/mpc-drumpad"; : > "$R/data/mpc-drumpad/MPC"; : > "$R/data/mpc-drumpad/STOCK"
    if [ "$1" = fits ]; then
        printf '#!/bin/sh\n[ "$1" = reapply ] || exit 1\nmkdir -p "$AR_ROOT/run/systemd/system/acvs.service.d"\necho "[Service]" > "$AR_ROOT/run/systemd/system/acvs.service.d/94-mpc-drumpad.conf"\necho "reapply: drop-in written"\n' > "$R/data/mpc-drumpad/mpc-drum-pad-patch.sh"
    else
        printf '#!/bin/sh\necho "reapply: the saved copy is for a different MPC than this system runs; leaving stock"\n' > "$R/data/mpc-drumpad/mpc-drum-pad-patch.sh"
    fi
}
run() { AR_ROOT="$R" /tmp/ar_driver /tmp/addin_restore_test.so; }
CONF=run/systemd/system/acvs.service.d/90-mpc-addins.conf
DP=run/systemd/system/acvs.service.d/94-mpc-drumpad.conf
check() {   # name, condition (shell), on the current $R
    n=$((n + 1))
    if (cd "$R" && eval "$2"); then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); sed 's/^/     log: /' "$R/tmp/addin-restore.log" 2>/dev/null || true; fi
}

fresh; run
check "nothing installed: guard ok, no drop-in, no restart" '[ "$(cat run/addin-restore-done)" = ok ] && [ ! -e $CONF ] && [ ! -e restarted ]'

fresh; addin a a.so; echo restarting > "$R/run/addin-restore-done"; run
check "guard present: does nothing" '[ ! -e $CONF ] && [ ! -e restarted ]'

fresh; addin a a.so; addin b b.so; env_preload "/usr/lib/x.so:/data/mpc-addins/a/a.so"; run
check "one addin missing: kept order, appended, one restart" \
  'grep -qx "Environment=LD_PRELOAD=/usr/lib/x.so:/data/mpc-addins/a/a.so:/data/mpc-addins/b/b.so" $CONF && head -n 1 $CONF | grep -qx "\[Service\]" && [ -e restarted ] && [ "$(cat run/addin-restore-done)" = restarting ]'

fresh; addin a a.so; run
check "no LD_PRELOAD: no leading colon" 'grep -qx "Environment=LD_PRELOAD=/data/mpc-addins/a/a.so" $CONF && [ -e restarted ]'

fresh; addin a a.so; mkdir -p "$R/data/mpc-addins/.hidden" "$R/data/mpc-addins/empty"; : > "$R/data/mpc-addins/.hidden/h.so"; : > "$R/data/mpc-addins/a/readme.txt"; run
check "dot folders, folders without a .so and non-.so files are skipped" 'grep -qx "Environment=LD_PRELOAD=/data/mpc-addins/a/a.so" $CONF'

fresh; addin a a.so; env_preload "/data/mpc-addins/a/a.so"; run
check "all addins loaded: no drop-in, no restart" '[ ! -e $CONF ] && [ ! -e restarted ] && [ "$(cat run/addin-restore-done)" = ok ]'

fresh; drumpad fits; run
check "16-pad installed, no addins: reapply, one restart" '[ -e $DP ] && [ ! -e $CONF ] && [ -e restarted ] && grep -q "16-pad drop-in written" tmp/addin-restore.log'

fresh; drumpad refuses; run
check "16-pad copy does not fit this MPC: stays stock, no restart" '[ ! -e $DP ] && [ ! -e restarted ] && grep -q "16-pad left stock" tmp/addin-restore.log'

fresh; drumpad fits; mkdir -p "$R/run/systemd/system/acvs.service.d"; echo keep > "$R/$DP"; run
check "16-pad drop-in already there: not run again, no restart" '[ "$(cat $DP)" = keep ] && [ ! -e restarted ]'

fresh; drumpad fits; addin a a.so; env_preload "/data/mpc-addins/a/a.so"; run
check "addins loaded, 16-pad not active: restart for the 16-pad only" '[ -e $DP ] && [ ! -e $CONF ] && [ -e restarted ]'

fresh; drumpad fits; addin a a.so; run
check "both missing: both drop-ins, still one restart" '[ -e $DP ] && [ -e $CONF ] && [ "$(cat restarted)" = 1 ]'

fresh; mkdir -p "$R/data/mpc-drumpad"; : > "$R/data/mpc-drumpad/MPC"; run
check "16-pad half there (no saved script): left alone" '[ ! -e $DP ] && [ ! -e restarted ]'

hook() {   # $1 addin, $2 manifest value, $3 write the script (yes/no)
    mkdir -p "$R/data/mpc-addins/$1"; : > "$R/data/mpc-addins/$1/$1.so"
    printf 'ADDIN_ID=%s\nADDIN_BOOT=%s\n' "$1" "$2" > "$R/data/mpc-addins/$1/addin.manifest"
    [ "$3" = yes ] && printf '#!/bin/sh\n[ "$1" = boot ] || exit 3\necho "$(pwd)" > "$AR_ROOT/hook-%s"\n' "$1" > "$R/data/mpc-addins/$1/hook.sh"
    return 0
}

fresh; hook a hook.sh yes; env_preload "/data/mpc-addins/a/a.so"; run
check "boot hook, addins loaded: hook runs with boot from its folder, no restart" \
  '[ "$(cat hook-a)" = "$R/data/mpc-addins/a" ] && [ ! -e restarted ] && [ "$(cat run/addin-restore-done)" = ok ] && grep -q "boot hook exit 0" tmp/addin-restore.log'

fresh; hook a hook.sh yes; run
check "boot hook and a missing addin: hook runs and one restart" '[ -e hook-a ] && [ -e restarted ] && [ -e $CONF ]'

fresh; hook a '"hook.sh"' yes; env_preload "/data/mpc-addins/a/a.so"; run
check "boot hook name in quotes" '[ -e hook-a ]'

fresh; hook a ../b/hook.sh yes; hook b x yes; env_preload "/data/mpc-addins/a/a.so:/data/mpc-addins/b/b.so"; run
check "boot hook with a path, or naming no file: not run" '[ ! -e hook-a ] && [ ! -e hook-b ] && grep -q "boot hook missing" tmp/addin-restore.log'

fresh; hook a .hidden yes; env_preload "/data/mpc-addins/a/a.so"; run
check "boot hook starting with a dot: not run" '[ ! -e hook-a ] && [ "$(cat run/addin-restore-done)" = ok ]'

fresh; hook a hook.sh yes; echo ok > "$R/run/addin-restore-done"; run
check "guard present: boot hook not run again" '[ ! -e hook-a ]'

echo "$((n - fails))/$n passed"
[ "$fails" = 0 ]
