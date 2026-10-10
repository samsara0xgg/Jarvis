import Foundation

/// What the app and its share extension both read to know who they are paired with. Compiled into both targets.
/// The token is in the Keychain's shared access group (see `Keychain`); the brain's addresses are in the App Group's defaults.
enum BrainStore {
    static let group = "group.com.yilunshi.jarvis"
    /// the App Group's defaults; the app's own when the group is not signed in (a build without the entitlement keeps working, only the extension cannot see it)
    static let defaults = UserDefaults(suiteName: group) ?? .standard

    enum Key { static let addresses = "brain.addresses", device = "brain.device", token = "brain.token" }

    /// Before the App Group existed the addresses and the phone's name were in the app's standard defaults: moved once, so a phone paired earlier stays paired.
    static func adopt(from old: UserDefaults = .standard, into new: UserDefaults = defaults) {
        guard new !== old else { return }
        for k in [Key.addresses, Key.device] {
            guard let v = old.object(forKey: k) else { continue }
            if new.object(forKey: k) == nil { new.set(v, forKey: k) }
            old.removeObject(forKey: k)
        }
    }

    /// The paired brain's addresses and this phone's token; nil until paired.
    static var pairing: (bases: [URL], token: String)? {
        guard let token = Keychain.get(Key.token), let a = defaults.stringArray(forKey: Key.addresses) else { return nil }
        let bases = a.compactMap(URL.init(string:))
        return bases.isEmpty ? nil : (bases, token)
    }
}
