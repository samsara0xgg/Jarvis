import SwiftUI

// One outing's page (A2), slid in over the one-day screen from its row. The integrator replaces this placeholder with the real page; the signature is the contract.

struct FootprintOutingPage: View {
    let model: FootprintModel
    let outing: FootprintOuting
    let back: () -> Void

    var body: some View {
        Color.clear.contentShape(Rectangle()).overlay(alignment: .topLeading) {
            Button(action: back) {
                Image(systemName: "chevron.left").font(.system(size: 17, weight: .semibold)).foregroundStyle(SetColor.ink).frame(width: 44, height: 44)
            }
            .buttonStyle(PressScale()).padding(.top, 57).padding(.leading, 60)
        }
    }
}
