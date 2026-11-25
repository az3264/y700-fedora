#!/bin/bash
# Point ostree-prepare-root at a deployment that exists when /KERNEL's baked
# ostree= karg names one that does not: ostree deletes the old boot.N tree on
# each deployment write, so a /KERNEL that missed its regen names a dead path.
# Same bootcsum means the same kernel, so it is a deployment this boot may
# enter. Only the karg is corrected; the deployment tree is ostree's.
#
# The boot image also bakes armada.deployment=, which would pin this device to
# one deployment forever: an rpm layered or a bootc upgrade staged on the device
# is ranked first by ostree and would never be entered. When the deployment
# ostree ranks first runs this boot image's kernel, follow it and carry the
# corrected binding into the cmdline userspace sees, so the update stack and the
# booted deployment stay consistent.

set -u
IFS=$' \t\n\v\f\r'

MARKER=/run/armada/ostree-fallback-remapped

log() { echo "armada-ostree-fallback: $*" > /dev/kmsg 2>/dev/null || echo "armada-ostree-fallback: $*"; }

# Echo the <csum>.<deployserial> a karg names, for both accepted pointer shapes.
armada_deployment_from_path() {
    local target
    case "${1}" in
        /ostree/armada/*) printf '%s\n' "${1##*/}" ;;
        /ostree/boot.*)
            target=$(readlink "/sysroot${1}" 2>/dev/null) || return 1
            printf '%s\n' "${target##*/}"
            ;;
        *) return 1 ;;
    esac
}

# Echo the bootcsum directory holding the bootlink that points at a deployment.
armada_bootcsum_for_deployment() {
    local want=$1 link target
    for link in /sysroot/ostree/boot.0/*/*/* /sysroot/ostree/boot.1/*/*/*; do
        [ -L "${link}" ] || continue
        target=$(readlink "${link}") || continue
        case "${target##*/}" in
            "${want}") printf '%s\n' "${link%/*}" ; return 0 ;;
        esac
    done
    return 1
}

# ostree ranks its deployments in /boot/loader/entries: the highest "ostree-N"
# entry is the one it boots. Print "<ostree karg> <csum>.<deployserial>" for it,
# but only while it shares this boot image's kernel (same bootcsum).
# Publish the stable /ostree/armada/<commit>.<serial> pointer for a deployment.
# The sysroot is mounted read-only, so remount it only for the symlink write and
# restore the read-only state afterwards.
armada_write_root_binding() {
    local osname=$1 deploy=$2 dir=/sysroot/ostree/armada remounted=0

    mkdir -p "${dir}" 2>/dev/null || true
    if ln -sfn "../deploy/${osname}/deploy/${deploy}" "${dir}/${deploy}" 2>/dev/null; then
        [ -L "${dir}/${deploy}" ] && return 0
    fi

    if mount -o remount,rw /sysroot 2>/dev/null; then
        remounted=1
    fi
    mkdir -p "${dir}" 2>/dev/null || true
    ln -sfn "../deploy/${osname}/deploy/${deploy}" "${dir}/${deploy}" 2>/dev/null || true
    [ "${remounted}" -eq 1 ] && mount -o remount,ro /sysroot 2>/dev/null

    [ -L "${dir}/${deploy}" ]
}

armada_ostree_default_deployment() {
    local cmdline=$1 bound_path=$2 boot_device="" token name num
    local bound_deploy="" bound_bc="" best_num=-1 best_conf="" best_karg=""
    local best_deploy="" best_bc=""

    for token in ${cmdline}; do
        case "${token}" in
            boot=*) boot_device="${token#boot=}" ;;
        esac
    done
    [ -n "${boot_device}" ] || return 1

    bound_deploy=$(armada_deployment_from_path "${bound_path}") || return 1
    bound_bc=$(armada_bootcsum_for_deployment "${bound_deploy}") || return 1
    bound_bc=${bound_bc##*/}
    [ -n "${bound_bc}" ] || return 1

    mkdir -p /run/armada/boot 2>/dev/null || true
    mount -o ro "${boot_device}" /run/armada/boot 2>/dev/null || return 1

    for name in /run/armada/boot/loader/entries/ostree-*.conf; do
        [ -f "${name}" ] || continue
        num=${name##*/ostree-}
        num=${num%%[!0-9]*}
        case "${num}" in ''|*[!0-9]*) continue ;; esac
        if [ "${num}" -gt "${best_num}" ]; then
            best_num=${num}
            best_conf=${name}
        fi
    done

    if [ -n "${best_conf}" ]; then
        set -f
        while IFS= read -r token; do
            case "${token}" in
                options\ *)
                    for name in ${token#options }; do
                        case "${name}" in
                            ostree=*) best_karg=${name#ostree=} ;;
                        esac
                    done
                    ;;
            esac
        done < "${best_conf}"
        set +f
    fi
    umount /run/armada/boot 2>/dev/null || true

    [ -n "${best_karg}" ] || return 1
    best_deploy=$(armada_deployment_from_path "${best_karg}") || return 1
    best_bc=$(armada_bootcsum_for_deployment "${best_deploy}") || return 1
    best_bc=${best_bc##*/}
    [ "${best_bc}" = "${bound_bc}" ] || return 1

    printf '%s %s\n' "${best_karg}" "${best_deploy}"
}

# ExecStartPost: /proc/cmdline is conventionally what the bootloader passed, and
# the stale value is the evidence that this device was rescued.
if [ "${1:-}" = "--restore" ]; then
    [ -e "${MARKER}" ] || exit 0
    umount /proc/cmdline 2>/dev/null || log "could not restore the real /proc/cmdline"
    exit 0
fi

read -r cmdline < /proc/cmdline

set -f
karg=""
binding=""
bound=0
soft=0
for t in ${cmdline}; do
    case "${t}" in
        ostree=*) karg="${t#ostree=}" ;;
        armada.deployment=*) bound=1; binding="${t#armada.deployment=}" ;;
        armada.deployment) bound=1 ;;
    esac
done
set +f

if [ "${bound}" -eq 1 ]; then
    if ! found=$(/usr/libexec/armada/armada-bound-deployment "${cmdline}" /sysroot); then
        log "explicit deployment binding could not be resolved"
        exit 1
    fi
    [ -n "${found}" ] || { log "deployment selector returned an empty path"; exit 1; }

    # ostree keeps its own default: a deployment created on the device (rpm
    # layered, bootc upgrade) is ranked first in /boot/loader/entries, while the
    # baked binding still names the image's original deployment. Follow ostree
    # whenever that default runs this boot image's kernel (same bootcsum).
    bound_deploy=$(armada_deployment_from_path "${found}" 2>/dev/null) || bound_deploy=""
    if [ -n "${bound_deploy}" ] && \
       newer=$(armada_ostree_default_deployment "${cmdline}" "${found}"); then
        newer_deploy=${newer##* }
        if [ -n "${newer_deploy}" ] && [ "${newer_deploy}" != "${bound_deploy}" ]; then
            osname=${binding%%/*}
            case "${osname}" in ''|*/*) osname=default ;; esac
            # Keep the pointer shape the boot image baked: the update stack
            # validates the cmdline against the deployment and accepts only the
            # shape this boot medium was built for.
            case "${karg}" in
                /ostree/armada/*)
                    # The sysroot is mounted read-only; the stable pointer has
                    # to exist in the sysroot itself so userspace validation can
                    # canonicalize it.  Remount only for the symlink write.
                    armada_write_root_binding "${osname}" "${newer_deploy}" || true
                    if [ -L "/sysroot/ostree/armada/${newer_deploy}" ]; then
                        found="/ostree/armada/${newer_deploy}"
                    else
                        log "cannot publish /ostree/armada/${newer_deploy}; using the bootlink pointer"
                        found=${newer%% *}
                    fi
                    ;;
                *) found=${newer%% *} ;;
            esac
            binding="${osname}/${newer_deploy}"
            soft=1
            log "ostree ranks ${newer_deploy} first; following it instead of the baked ${bound_deploy}"
        fi
    fi
    [ "${found}" = "${karg}" ] && [ "${soft}" -eq 0 ] && exit 0
else
    [ -n "${karg}" ] || exit 0
    [ -e "/sysroot${karg}" ] && exit 0
    [ -d /sysroot/ostree ] || exit 0

    case "${karg}" in /ostree/boot.*) ;; *) exit 0 ;; esac
    rel="${karg#/ostree/}"
    bootdir="${rel%%/*}"; rel="${rel#*/}"
    osname="${rel%%/*}"; rel="${rel#*/}"
    csum="${rel%%/*}"
    idx="${rel#*/}"
    case "${idx}" in */*) idx="" ;; esac
    if [ -z "${bootdir}" ] || [ -z "${osname}" ] || [ -z "${csum}" ] || \
       [ -z "${idx}" ] || [ "${csum}" = "${idx}" ]; then
        log "unparseable ostree= karg: ${karg}"
        exit 0
    fi

    log "baked ostree path ${karg} is missing; looking for bootcsum ${csum}"

    # Same index under the surviving bootversion first, then any index for this
    # bootcsum: the kernel and initramfs are identical either way.
    found=""
    for b in boot.0 boot.1; do
        if [ -e "/sysroot/ostree/${b}/${osname}/${csum}/${idx}" ]; then
            found="/ostree/${b}/${osname}/${csum}/${idx}"
            break
        fi
    done
    if [ -z "${found}" ]; then
        for e in /sysroot/ostree/boot.0/"${osname}/${csum}"/* \
                 /sysroot/ostree/boot.1/"${osname}/${csum}"/*; do
            [ -e "${e}" ] || continue
            found="${e#/sysroot}"
            break
        done
    fi
    if [ -z "${found}" ]; then
        log "no surviving bootlink for bootcsum ${csum}; cannot remap"
        exit 0
    fi
fi

set -f
fixed=""
for t in ${cmdline}; do
    case "${t}" in
        ostree=*) fixed="${fixed} ostree=${found}" ;;
        armada.deployment=*)
            if [ -n "${binding}" ]; then
                fixed="${fixed} armada.deployment=${binding}"
            else
                fixed="${fixed} ${t}"
            fi
            ;;
        *) fixed="${fixed} ${t}" ;;
    esac
done
set +f
fixed="${fixed# }"

failure_status=0
[ "${bound}" -eq 0 ] || failure_status=1

# A soft remap follows ostree's own default, so the corrected cmdline is the
# truth for this boot and stays visible: the update stack validates the booted
# deployment against it. The baked evidence is only rescued, so never fail boot.
if [ "${soft}" -eq 1 ]; then
    MARKER=/run/armada/ostree-default-remapped
    failure_status=0
fi

# Only for ostree-prepare-root's read; ExecStartPost puts the real one back.
mkdir -p /run/armada 2>/dev/null || true
if ! printf '%s\n' "${fixed}" > /run/armada/cmdline 2>/dev/null; then
    log "could not stage a corrected cmdline"
    exit "${failure_status}"
fi
# Marker before the mount: --restore keys off it, so a replacement must never
# outlive a marker that was never written.
if ! printf '%s\n' "${found}" > "${MARKER}" 2>/dev/null; then
    log "could not write the restore marker; leaving the baked karg alone"
    rm -f /run/armada/cmdline 2>/dev/null || true
    exit "${failure_status}"
fi
if mount --bind /run/armada/cmdline /proc/cmdline 2>/dev/null; then
    # /proc/cmdline is normally immutable; a writable bind would not be.
    if ! mount -o remount,bind,ro /proc/cmdline 2>/dev/null; then
        log "corrected /proc/cmdline is writable: read-only remount failed"
        if [ "${bound}" -eq 1 ] && [ "${soft}" -eq 0 ]; then
            if umount /proc/cmdline 2>/dev/null; then
                rm -f "${MARKER}" /run/armada/cmdline 2>/dev/null || log "could not remove staged cmdline state"
            else
                log "could not remove the writable cmdline replacement"
                rm -f /run/armada/cmdline 2>/dev/null || log "could not remove staged cmdline state"
            fi
            exit 1
        fi
    fi
    log "remapped ostree= to ${found}; /KERNEL is stale and is regenerated after boot"
else
    log "could not overmount /proc/cmdline"
    rm -f "${MARKER}" 2>/dev/null || log "stale restore marker remains at ${MARKER}"
    if [ "${bound}" -eq 1 ] && [ "${soft}" -eq 0 ]; then
        rm -f /run/armada/cmdline 2>/dev/null || log "writable alias remains at /run/armada/cmdline"
        exit 1
    fi
fi
# The mount holds the inode, so dropping the pathname leaves no writable alias:
# a read-only bind target does not make the source read-only.
rm -f /run/armada/cmdline 2>/dev/null || log "writable alias remains at /run/armada/cmdline"
exit 0
