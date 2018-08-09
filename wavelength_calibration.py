"""
Wavelength Calibration
by comparison to a reference spectrum
Loosely bases on the IDL wavecal function
"""

import numpy as np
from numpy.polynomial.polynomial import polyval2d
import logging

from scipy.io import readsav
from scipy.optimize import curve_fit
from scipy import signal
from scipy.constants import speed_of_light
import astropy.io.fits as fits

from instruments import instrument_info

import matplotlib.pyplot as plt
import util


class AlignmentPlot:
    """
    Makes a plot which can be clicked to align the two spectra, reference and observed
    """

    def __init__(self, ax, thar, cs_lines, offset=[0, 0]):
        self.im = ax
        self.first = True
        self.nord, self.ncol = thar.shape
        self.RED, self.GREEN, self.BLUE = 0, 1, 2

        self.thar = thar
        self.cs_lines = cs_lines

        self.order_first = 0
        self.spec_first = ""
        self.x_first = 0
        self.offset = offset

        self.make_ref_image()

    def make_ref_image(self):
        """ create and show the reference plot, with the two spectra """
        ref_image = np.zeros((self.nord * 2, self.ncol, 3))
        for iord in range(self.nord):
            idx = self.thar[iord] > 0.1
            ref_image[iord * 2, idx, self.RED] = self.thar[iord, idx]
            if 0 <= iord + self.offset[0] < self.nord:
                for line in self.cs_lines[self.cs_lines.order == iord]:
                    first = np.clip(line.xfirst + self.offset[1], 0, self.ncol)
                    last = np.clip(line.xlast + self.offset[1], 0, self.ncol)
                    ref_image[
                        (iord + self.offset[0]) * 2 + 1, first:last, self.GREEN
                    ] = line.height * signal.gaussian(last - first, line.width)

        self.im.imshow(
            ref_image,
            aspect="auto",
            origin="lower",
            extent=(0, self.ncol, 0, self.nord),
        )
        self.im.figure.suptitle(
            "Alignment, ThAr: RED, Reference: GREEN\nGreen should be above red!"
        )
        self.im.figure.canvas.draw()

    def connect(self):
        """ connect the click event with the appropiate function """
        self.cidclick = self.im.figure.canvas.mpl_connect(
            "button_press_event", self.on_click
        )

    def on_click(self, event):
        """ On click offset the reference by the distance between click positions """
        order = int(np.floor(event.ydata))
        spec = (
            "ref" if (event.ydata - order) > 0.5 else "thar"
        )  # if True then reference
        x = event.xdata
        print("Order: %i, Spectrum: %s, x: %g" % (order, "ref" if spec else "thar", x))

        # on every second click
        if self.first:
            self.first = False
            self.order_first = order
            self.spec_first = spec
            self.x_first = x
        else:
            # Clicked different spectra
            if spec != self.spec_first:
                self.first = True
                direction = -1 if spec == "ref" else 1
                offset_orders = int(order - self.order_first) * direction
                offset_x = int(x - self.x_first) * direction
                self.offset[0] += offset_orders
                self.offset[1] += offset_x
                self.make_ref_image()


def align(thar, cs_lines, manual=False, plot=False):
    """Align the observation with the reference spectrum
    Either automatically using cross correlation or manually (visually)

    Parameters
    ----------
    thar : array[nrow, ncol]
        observed wavelength calibration spectrum (e.g. ThAr=ThoriumArgon)
    cs_lines : struct_array
        reference line data
    manual : bool, optional
        wether to manually align the spectra (default: False)
    plot : bool, optional
        wether to plot the alignment (default: False)

    Returns
    -------
    offset: tuple(int, int)
        offset in order and column
    """

    # Align using window like in IDL REDUCE
    if manual:
        _, ax = plt.subplots()
        ap = AlignmentPlot(ax, thar, cs_lines)
        ap.connect()
        plt.show()
        offset = ap.offset
    else:
        # make image from cs_lines
        min_order = np.min(cs_lines.order)
        img = np.zeros_like(thar)
        for line in cs_lines:
            img[line.order, line.xfirst : line.xlast] = line.height * signal.gaussian(
                line.xlast - line.xfirst, line.width
            )
        img = np.ma.masked_array(img, mask=img == 0)

        # Cross correlate with thar image
        correlation = signal.correlate2d(thar, img, mode="same")
        offset_order, offset_x = np.unravel_index(
            np.argmax(correlation), correlation.shape
        )

        offset_order = 2 * offset_order - thar.shape[0] + min_order + 1
        offset_x = offset_x - thar.shape[1] / 2
        offset = int(offset_order), int(offset_x)

        if plot:
            _, ax = plt.subplots()
            AlignmentPlot(ax, thar, cs_lines, offset=offset)
            plt.show()

    return offset


def build_2d_solution(cs_lines, plot=False):
    """make a 2d polynomial fit to flagged lines
    
    Parameters
    ----------
    cs_lines : struc_array
        line data
    plot : bool, optional
        wether to plot the solution (default: False)
    
    Returns
    -------
    coef : array[degree_x, degree_y]
        2d polynomial coefficients
    """

    # Only use flagged data
    mask = ~cs_lines.flag.astype(bool) # 0 = True, 1 = False
    m_wave = cs_lines.wll[mask]
    m_pix = cs_lines.posm[mask]
    m_ord = cs_lines.order[mask]

    # 2d polynomial fit with: x = column, y = order, z = wavelength
    #coef = util.polyfit2d(m_pix, m_ord, m_wave, 5, plot=plot)

    degree_x, degree_y = 5, 5
    degree_x, degree_y = degree_x + 1, degree_y + 1  # Due to how np polyval2d workss

    def func(x, *c):
        c = np.array(c)
        c.shape = degree_x, degree_y
        value = polyval2d(x[0], x[1], c)
        return value

    coef, pcov = curve_fit(
        func, [m_pix, m_ord], m_wave, p0=np.ones(degree_x * degree_y)
    )
    coef.shape = degree_x, degree_y
    return coef

    


def make_wave(thar, wave_solution, plot=False):
    """Expand polynomial wavelength solution into full image

    Parameters
    ----------
    thar : array[nord, ncol]
        observed wavelength spectrum
    wave_solution : array[degree, degree]
        polynomial coefficients of wavelength solution
    plot : bool, optional
        wether to plot the solution (default: False)

    Returns
    -------
    wave_img : array[nord, ncol]
        wavelength solution for each point in the spectrum
    """

    nord, ncol = thar.shape
    x = np.arange(ncol)
    y = np.arange(nord)
    x, y = np.meshgrid(x, y)
    wave_img = polyval2d(x, y, wave_solution)

    if plot:
        plt.subplot(211)
        plt.xlabel("Wavelength")
        plt.ylabel("Thar spectrum")
        plt.legend(loc="best")
        for i in range(thar.shape[0]):
            plt.plot(wave_img[i], thar[i], label="Order %i" % i)

        plt.subplot(212)
        plt.imshow(wave_img, aspect="auto", origin="lower", extent=(0, ncol, 0, nord))
        cbar = plt.colorbar()
        plt.xlabel("Column")
        plt.ylabel("Order")
        cbar.set_label("Wavelength [Å]")
        plt.title("2D Wavelength solution")
        plt.show()

    return wave_img


def auto_id(thar, wave_img, cs_lines, threshold=1, plot=False):
    """Automatically identify peaks that are close to known lines 

    Parameters
    ----------
    thar : array[nord, ncol]
        observed spectrum
    wave_img : array[nord, ncol]
        wavelength solution image
    cs_lines : struc_array
        line data
    threshold : int, optional
        difference threshold between line positions in Angstrom, until which a line is considered identified (default: 1)
    plot : bool, optional
        wether to plot the new lines

    Returns
    -------
    cs_line : struct_array
        line data with new flags
    """

    # TODO: auto ID based on wavelength solution or position on detector?
    # Set flags in cs_lines
    nord, ncol = thar.shape

    # Step 1: find peaks in thar
    # Step 2: compare lines in cs_lines with peaks
    # Step 3: toggle flag on if close enough
    for iord in range(nord):
        # TODO pick good settings
        vec = thar[iord, thar[iord] > 0]
        vec -= np.min(vec)
        peak_idx = signal.find_peaks_cwt(
            vec, np.arange(2, 5), min_snr=5, min_length=3
        ).astype(int)
        pos_wave = wave_img[iord, thar[iord] > 0][peak_idx]

        for i, line in enumerate(cs_lines):
            if line.order == iord:
                diff = np.abs(line.wll - pos_wave)
                if np.min(diff) < threshold:
                    cs_lines.flag[i] = 0  # 0 == True, 1 == False

    return cs_lines


def reject_lines(thar, wave_solution, cs_lines, clip=100, plot=True):
    """
    Reject lines that are too far from the peaks
    
    Parameters
    ----------
    thar : array[nord, ncol]
        observed wavelength spectrum
    wave_solution : array[nord, ncol]
        current wavelength solution
    cs_lines : struct_array
        line data
    clip : int, optional
        clipping threshold in m/s (default: 100)
    plot : bool, optional
        wether to plot the results (default: False)
    
    Returns
    -------
    cs_lines : struct_array
        line data with new flags
    """

    # Calculate residuals
    nord, ncol = thar.shape
    x = cs_lines.posm
    y = cs_lines.order
    solution = polyval2d(x, y, wave_solution)
    # Residual in m/s
    residual = (solution - cs_lines.wll) / cs_lines.wll * speed_of_light
    cs_lines.flag[np.abs(residual) > clip] = 1  # 1 = False
    mask = ~cs_lines.flag.astype(bool)

    if plot:
        _, axis = plt.subplots()
        axis.plot(cs_lines.order[mask], residual[mask], "+")
        axis.plot(cs_lines.order[~mask], residual[~mask], "d")
        axis.set_xlabel("Order")
        axis.set_ylabel("Residual")
        axis.set_title("Residuals over order")

        fig, ax = plt.subplots(nrows=nord // 2, ncols=2, sharex=True)
        plt.subplots_adjust(hspace=0)
        fig.suptitle("Residuals over columns")

        for iord in range(nord):
            lines = cs_lines[cs_lines.order == iord]
            solution = polyval2d(lines.posm, lines.order, wave_solution)
            # Residual in m/s
            residual = (solution - lines.wll) / lines.wll * speed_of_light
            mask = ~lines.flag.astype(bool)
            ax[iord // 2, iord % 2].plot(lines.posm[mask], residual[mask], "+")
            ax[iord // 2, iord % 2].plot(lines.posm[~mask], residual[~mask], "d")
            # ax[iord // 2, iord % 2].tick_params(labelleft=False)
            ax[iord // 2, iord % 2].set_ylim(-clip * 1.5, +clip * 1.5)

        ax[-1, 0].set_xlabel("Column")
        ax[-1, 1].set_xlabel("Column")

        plt.show()

    return cs_lines


def wavecal(thar, cs_lines, plot=True, manual=False):
    """Wavelength calibration wrapper

    Parameters
    ----------
    thar : array[nord, ncol]
        observed wavelength spectrum
    cs_lines : struct_array
        line data
    plot : bool, optional
        wether to plot the final results (default: True)
    manual : bool, optional
        if True use manual alignment, otherwise automatic (default: False)

    Returns
    -------
    wave_img : array[nord, ncol]
        wavelength solution for each point in the spectrum
    """


    # normalize images
    thar = np.ma.masked_array(thar, mask=thar == 0)
    thar -= np.min(thar)
    thar /= np.max(thar)

    cs_lines.height /= np.max(cs_lines.height)

    # Step 1: align thar and reference
    offset = align(thar, cs_lines, plot=plot, manual=manual)
    # Step 1.5: Apply offset
    # be careful not to apply offset twice
    cs_lines.xfirst += offset[1]
    cs_lines.xlast += offset[1]
    cs_lines.posm += offset[1]
    cs_lines.order += offset[0]

    for j in range(3):
        for i in range(5):
            wave_solution = build_2d_solution(cs_lines)
            cs_lines = reject_lines(
                thar, wave_solution, cs_lines, plot=i == 4 and j == 2 and plot
            )

        wave_solution = build_2d_solution(cs_lines)
        wave_img = make_wave(thar, wave_solution)

        cs_lines = auto_id(thar, wave_img, cs_lines)

    logging.info(
        "Number of lines used for wavelength calibration: %i",
        len(cs_lines[~cs_lines.flag.astype(bool)]),
    )

    # Step 6: build final 2d solution
    wave_solution = build_2d_solution(cs_lines, plot=plot)
    wave_img = make_wave(thar, wave_solution, plot=plot)

    return wave_img  # wavelength solution image


if __name__ == "__main__":
    instrument = "UVES"
    mode = "middle"
    target = "HD132205"
    night = "2010-04-02"

    thar_file = "./Test/UVES/{target}/reduced/{night}/Reduced_{mode}/UVES.2010-04-02T11_09_23.851.thar.ech"
    thar_file = thar_file.format(
        instrument=instrument, mode=mode, night=night, target=target
    )

    thar = fits.open(thar_file)
    head = thar[0].header
    thar = thar[1].data["SPEC"][0]

    reference = instrument_info.get_wavecal_filename(head, instrument, mode)
    reference = readsav(reference)

    # cs_lines = "Wavelength_center", "Wavelength_left", "Position_Center", "Position_Middle", "first x coordinate", "last x coordinate", "approximate ??", "width", "flag", "height", "order"
    # cs_lines = 'WLC', 'WLL', 'POSC', 'POSM', 'XFIRST', 'XLAST', 'APPROX', 'WIDTH', 'FLAG', 'HEIGHT', 'ORDER'
    cs_lines = reference["cs_lines"]

    # list of orders, bad orders marked with 1, normal orders 0
    bad_order = reference["bad_order"]

    solution = wavecal(thar, cs_lines, plot=False)

    for i in range(thar.shape[0]):
        plt.plot(solution[i], thar[i], label="Order %i" % i)

    for line in cs_lines:
        plt.axvline(x=line.wll, ymax=line.height)

    plt.show()

    print(solution)
